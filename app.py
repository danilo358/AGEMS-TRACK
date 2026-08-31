import requests
import re
import threading
from datetime import datetime, timezone, date
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, make_response
import io
import pandas as pd
import itertools
import math
from geopy.distance import geodesic
import xml.etree.ElementTree as ET
import uuid
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

# =========================================================
# CONFIGURAÇÃO FLASK & BANCO DE DADOS
# =========================================================
from flask_sqlalchemy import SQLAlchemy

def carregar_env_local():
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for linha in env_path.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        nome, valor = linha.split("=", 1)
        os.environ.setdefault(nome.strip(), valor.strip().strip('"').strip("'"))

carregar_env_local()

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("AGEMS_SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("Defina a variável de ambiente AGEMS_SECRET_KEY antes de iniciar a aplicação.")
database_url = os.environ.get("DATABASE_URL", "sqlite:///agems_v2.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
elif database_url.startswith("postgresql://") and "+psycopg" not in database_url:
    database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Modelo para Cadastro Mestre de Veículos
class Veiculo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    placa = db.Column(db.String(10), unique=True, index=True)
    prefixo = db.Column(db.String(50))
    empresa = db.Column(db.String(200))
    ultima_comunicacao = db.Column(db.String(100))
    observacao = db.Column(db.String(1000), default="")
    precisa_manutencao = db.Column(db.Boolean, default=False)
    manutencao_manual = db.Column(db.Boolean, default=False)
    data_atualizacao = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

# Modelo para o Relatório Diário (Auditoria)
class VeiculoAudit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    placa = db.Column(db.String(10), index=True)
    prefixo = db.Column(db.String(50))
    empresa = db.Column(db.String(200))
    tipo_relatorio = db.Column(db.String(50)) # instalacao, manutencao, desinstalacao
    motivo = db.Column(db.String(500))
    observacao_mestre = db.Column(db.String(1000)) # Puxado do cadastro mestre
    dias_offline = db.Column(db.Integer, nullable=True)
    ultima_posicao = db.Column(db.String(100), nullable=True)
    data_auditoria = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

# Modelo para o Responsável de cada Empresa
class EmpresaResponsavel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa = db.Column(db.String(200), unique=True, index=True)
    titulo = db.Column(db.String(20)) # Sr, Sra, Desconhecido
    nome = db.Column(db.String(200))

# Snapshot persistido da última coleta completa da frota.
class RelatorioSnapshot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    gerado_em = db.Column(db.DateTime(timezone=True), nullable=False)
    ativos_vistoria_total = db.Column(db.JSON, nullable=False, default=list)
    ativos_vistoria_com_rastreador = db.Column(db.JSON, nullable=False, default=list)
    ativos_vistoria_sem_rastreador = db.Column(db.JSON, nullable=False, default=list)


with app.app_context():
    db.create_all()
    try:
        from sqlalchemy import text
        db.session.execute(text("ALTER TABLE veiculo ADD COLUMN precisa_manutencao BOOLEAN DEFAULT 0"))
        db.session.commit()
    except Exception:
        db.session.rollback()
    try:
        db.session.execute(text("ALTER TABLE veiculo ADD COLUMN manutencao_manual BOOLEAN DEFAULT 0"))
        db.session.commit()
    except Exception:
        db.session.rollback()

# Estado global dos relatórios
_report_cache = {}
_report_lock = threading.Lock()
_report_status = {"status": "idle", "log": [], "error": None}

# =========================================================
# FUNÇÕES DE UTILIDADE E LOGIN
# =========================================================
def normalizar_nome(nome):
    if not nome: return ""
    return re.sub(r'\s+', ' ', nome).strip().upper()

FUSO_RELATORIO = ZoneInfo("America/Campo_Grande")

def agora_utc():
    return datetime.now(timezone.utc)

def parse_data_api(valor, nome_campo):
    if not valor:
        return None
    try:
        texto = str(valor).strip()
        data = datetime.fromisoformat(texto.replace("Z", "+00:00"))
        if data.tzinfo is None:
            data = data.replace(tzinfo=timezone.utc)
        return data.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Data inválida recebida em {nome_campo}: {valor!r}") from exc

def parse_ultima_comunicacao(valor):
    if not valor:
        return None
    try:
        return datetime.strptime(valor, "%d/%m/%Y %H:%M").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None

def data_vistoria_valida(valor, hoje):
    if not valor:
        return False
    texto = str(valor).strip()
    try:
        data = datetime.fromisoformat(texto.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            data = datetime.strptime(texto, "%d/%m/%Y").date()
        except ValueError as exc:
            raise ValueError(f"Data de vencimento de vistoria inválida: {valor!r}") from exc
    return data >= hoje

def deve_entrar_manutencao(dias_offline, manutencao_manual):
    return manutencao_manual or (dias_offline is not None and dias_offline >= 15)

def deve_entrar_desinstalacao(is_inativo, is_desat_pendente, is_empresa_regular, has_tracker):
    return has_tracker and (is_inativo or is_desat_pendente or not is_empresa_regular)

def formatar_data_relatorio(data=None):
    data = data or agora_utc()
    return data.astimezone(FUSO_RELATORIO).strftime("%d/%m/%Y às %H:%M")

def limpar_placa(placa):
    """
    Sanitiza a placa removendo caracteres especiais e convertendo para maiúsculas.
    Ex: 'fgw3h41' -> 'FGW3H41'
    """
    if not placa: return ""
    return re.sub(r'[^A-Z0-9]', '', str(placa).strip().upper())

def normalizar_placa_mercosul(placa):
    """
    Normaliza placas Mercosul para o formato antigo para comparação.
    O 5º caractere (índice 4) é substituído pelo número correspondente.
    A=0, B=1, C=2, D=3, E=4, F=5, G=6, H=7, I=8, J=9
    """
    placa = limpar_placa(placa)
    if len(placa) != 7:
        return placa

    mapa = {'A': '0', 'B': '1', 'C': '2', 'D': '3', 'E': '4', 'F': '5', 'G': '6', 'H': '7', 'I': '8', 'J': '9'}

    # Se o 5º caractere for letra, troca pelo número
    char_5 = placa[4]
    if char_5.isalpha():
        char_sub = mapa.get(char_5, char_5)
        return placa[:4] + char_sub + placa[5:]

    return placa

def fazer_login_agems(email, senha):
    url_auth = "https://www.monitora.ms.gov.br/auth"
    payload = {
        "operationName": "Login_Auth",
        "variables": {"email": email, "senha": senha},
        "query": "mutation Login_Auth($email: String!, $senha: String!) {\n  login(input: {email: $email, senha: $senha}) {\n    token\n  }\n}"
    }
    headers = {
        "content-type": "application/json",
        "apollo-require-preflight": "true",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    res = requests.post(url_auth, json=payload, headers=headers, timeout=20)
    res.raise_for_status()
    return res.json().get('data', {}).get('login', {}).get('token')

def login_systemsat():
    hash_auth = os.environ.get("SYSTEMSAT_HASH_AUTH")
    username = os.environ.get("SYSTEMSAT_USERNAME")
    password = os.environ.get("SYSTEMSAT_PASSWORD")
    if not all((hash_auth, username, password)):
        raise RuntimeError("Defina SYSTEMSAT_HASH_AUTH, SYSTEMSAT_USERNAME e SYSTEMSAT_PASSWORD.")
    url = "https://integration.systemsatx.com.br/Login"
    headers = { "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" }
    res = requests.post(url, params={"HashAuth": hash_auth, "Username": username, "Password": password}, headers=headers, timeout=15)
    res.raise_for_status()
    return res.json().get("AccessToken")

# =========================================================
# FUNÇÕES DE EXTRAÇÃO DE DADOS
# =========================================================
def buscar_todos_veiculos(token):
    url_vistoria = "https://www.monitora.ms.gov.br/vistoria/"
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "referer": "https://www.monitora.ms.gov.br/",
        "origin": "https://www.monitora.ms.gov.br"
    }
    payload = {
        "operationName": "BuscarRelatorioVeiculos",
        "variables": {"page": 1, "paginate": False, "nome": "%%", "placa": "%%", "status": ""},
        "query": """query BuscarRelatorioVeiculos($paginate: Boolean, $page: Float, $placa: String, $nome: String, $status: String, $ativo: Boolean, $dataCriacaoFim: String, $dataCriacaoInicio: String) {
          buscarRelatorioVeiculos(pageOptionsDto: {paginate: $paginate, page: $page}, filtros: {nome: $nome, placa: $placa, status: $status, numeroChassi: $placa, ativo: $ativo, dataCriacaoInicio: $dataCriacaoInicio, dataCriacaoFim: $dataCriacaoFim}) {
            data { placa, veiculoStatus, ativo, empresa, dataVencimentoVistoria, prefixo }
          }
        }"""
    }
    res = requests.post(url_vistoria, json=payload, headers=headers, timeout=30)
    res.raise_for_status()
    resposta = res.json()
    resultado = resposta.get("data", {}).get("buscarRelatorioVeiculos")
    if not isinstance(resultado, dict) or not isinstance(resultado.get("data"), list):
        raise ValueError("Resposta da AGEMS sem a lista de veículos esperada.")
    lista_veiculos = resultado["data"]
    if not lista_veiculos:
        raise ValueError("A AGEMS retornou zero veículos; relatório não será substituído.")
    registros_por_placa = {}
    for v in lista_veiculos:
        placa_raw = v.get("placa")
        if not placa_raw: continue
        placa = limpar_placa(placa_raw)
        if not placa: continue
        is_ativo = v.get("ativo", False)
        registro = {
            "placa_original": placa_raw,
            "ativo": is_ativo,
            "status": v.get("veiculoStatus"),
            "prefixo": v.get("prefixo") or "",
            "empresa": normalizar_nome(v.get("empresa", "")),
            "vencimento_vistoria": v.get("dataVencimentoVistoria")
        }
        registros_por_placa.setdefault(placa, []).append(registro)

    veiculos_mestre = {}
    for placa, ocorrencias in registros_por_placa.items():
        principal = next((registro for registro in ocorrencias if registro["ativo"]), ocorrencias[0])
        principal["ocorrencias"] = ocorrencias
        veiculos_mestre[placa] = principal
    return veiculos_mestre

def buscar_empresas_com_pasta_valida(token):
    headers = {
        "content-type": "application/json", "authorization": f"Bearer {token}",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "referer": "https://www.monitora.ms.gov.br/", "origin": "https://www.monitora.ms.gov.br"
    }
    url = "https://www.monitora.ms.gov.br/linha"
    empresas_com_pasta = set()
    page = 1
    has_more = True
    data_hoje = date.today()

    while has_more:
        payload = {
            "operationName": "BuscarPastas_Linha",
            "variables": {"filtros": {}, "page": float(page)},
            "query": """query BuscarPastas_Linha($filtros: FiltroBuscarPastasInput, $page: Float) {
              buscarPastas(filtros: $filtros, pageOptionsDto: {paginate: true, page: $page, take: 50}) {
                data {
                  id
                  numero
                  descricao
                  empresa {
                    nomeFantasia
                    razaoSocial
                  }
                  ordemServicos {
                    id
                    numero
                    vencimento
                  }
                }
                meta
              }
            }"""
        }
        try:
            res = requests.post(url, json=payload, headers=headers, timeout=30)
            res.raise_for_status()
            resposta = res.json()
            buscar_pastas_node = resposta.get("data", {}).get("buscarPastas") or {}
            pastas = buscar_pastas_node.get("data") or []
            meta = buscar_pastas_node.get("meta") or {}

            if not pastas:
                break

            for pasta in pastas:
                datas_validade = [
                    os_item.get("vencimento")
                    for os_item in pasta.get("ordemServicos") or []
                ]
                is_valida = False
                for data_validade in datas_validade:
                    if data_validade:
                        try:
                            if data_vistoria_valida(data_validade, data_hoje):
                                is_valida = True
                                break
                        except (TypeError, ValueError):
                            continue

                if is_valida:
                    emp_obj = pasta.get("empresa") or {}
                    nome_empresa = emp_obj.get("razaoSocial") or emp_obj.get("nomeFantasia") or pasta.get("descricao")
                    razao = normalizar_nome(nome_empresa)
                    if razao:
                        empresas_com_pasta.add(razao)

            has_next = meta.get("hasNextPage") if isinstance(meta, dict) else False
            page_count = meta.get("pageCount", 1) if isinstance(meta, dict) else 1
            if has_next or (page < page_count):
                page += 1
            else:
                has_more = False
        except Exception as e:
            print(f"[Monitora] Erro ao buscar pastas de empresas: {e}")
            break

    return empresas_com_pasta

def buscar_empresas_regulares(token):
    return buscar_empresas_com_pasta_valida(token)

def buscar_pedidos_desativacao(token):
    url_vistoria = "https://www.monitora.ms.gov.br/vistoria/"
    headers = {
        "content-type": "application/json", "authorization": f"Bearer {token}",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "referer": "https://www.monitora.ms.gov.br/", "origin": "https://www.monitora.ms.gov.br"
    }
    query = """query solicitacoesAtivacoesDesativacoesVeiculos_vistoria($veiculo: String, $paging: CursorPaging) {
      solicitacoesAtivacoesDesativacoesVeiculos(filter: {veiculo: {or: [{placa: {iLike: $veiculo}}, {numeroChassi: {iLike: $veiculo}}, {prefixo: {iLike: $veiculo}}]}}, paging: $paging, sorting: {field: createdAt, direction: DESC}) {
        edges {
        node {
            motivo,
            tipoSolicitacao,
            aprovado,
            quemAnalisouId,
            veiculo { placa empresa { razaoSocial } }
          }
        }
        pageInfo { endCursor, hasNextPage }
      }
    }"""
    tem_proxima, cursor_atual, historico = True, None, {}
    while tem_proxima:
        paging_param = {"first": 50}
        if cursor_atual: paging_param["after"] = cursor_atual
        payload = {
            "operationName": "solicitacoesAtivacoesDesativacoesVeiculos_vistoria",
            "variables": {"paging": paging_param, "veiculo": "%%"},
            "query": query
        }
        resposta_http = requests.post(url_vistoria, json=payload, headers=headers, timeout=30)
        resposta_http.raise_for_status()
        res = resposta_http.json()
        solic_node = res.get("data", {}).get("solicitacoesAtivacoesDesativacoesVeiculos", {})
        if not isinstance(solic_node, dict) or not isinstance(solic_node.get("edges"), list) or not isinstance(solic_node.get("pageInfo"), dict):
            raise ValueError("Resposta da AGEMS sem a estrutura de solicitações esperada.")
        for edge in solic_node.get("edges", []):
            node = edge.get("node", {})
            placa_raw = node.get("veiculo", {}).get("placa")
            if not placa_raw: continue
            placa = limpar_placa(placa_raw)
            if not placa: continue

            # Filtro manual no Python para garantir que pegamos apenas o que é realmente pendente
            is_desat = node.get("tipoSolicitacao") == "DESATIVACAO"
            is_pendente = node.get("aprovado") is None and node.get("quemAnalisouId") is None

            if is_desat and is_pendente:
                empresa_obj = (node.get("veiculo") or {}).get("empresa")
                if isinstance(empresa_obj, dict):
                    empresa = normalizar_nome(empresa_obj.get("razaoSocial"))
                else:
                    empresa = normalizar_nome(empresa_obj)
                historico.setdefault(placa, []).append({
                    "empresa": empresa,
                    "motivo": node.get("motivo", "Sem motivo")
                })

        tem_proxima = solic_node.get("pageInfo", {}).get("hasNextPage", False)
        cursor_atual = solic_node.get("pageInfo", {}).get("endCursor")
    return historico

def selecionar_ocorrencia_veiculo(dados, pedidos):
    ocorrencias = dados.get("ocorrencias", [dados])
    pedidos = pedidos or []
    if isinstance(pedidos, dict):
        pedidos = [pedidos]

    ocorrencias_ativas = [ocorrencia for ocorrencia in ocorrencias if ocorrencia.get("ativo")]
    principal = ocorrencias_ativas[-1] if ocorrencias_ativas else ocorrencias[-1]

    if pedidos:
        return principal, pedidos[0].get("motivo", "Sem motivo")
    return principal, None

def rastreador_esta_vinculado_ssx(posicao):
    """Retorna False para registros de última posição que permanecem no SSX após a retirada do rastreador."""
    if not isinstance(posicao, dict):
        return False

    valores_status = []
    for campo in ("Status", "TrackedUnitStatus", "DeviceStatus", "VehicleStatus", "StatusDescription", "Ativo", "IsActive", "Enable", "Enabled", "TrackerStatus"):
        valor = posicao.get(campo)
        if valor is not None and valor != "":
            valores_status.append(str(valor))

    if posicao.get("IsActive") is False or posicao.get("Ativo") is False:
        return False
    if posicao.get("Enabled") is False or posicao.get("Enable") is False:
        return False
    if posicao.get("TrackerRemoved") is True or posicao.get("RastreadorRemovido") is True:
        return False

    texto_status = " ".join(valores_status).upper()
    tokens_invalidos = (
        "RETIRADO", "REMOVIDO", "DESATIVADO", "INATIVO", "DESLIGADO",
        "CANCELADO", "EXCLUIDO", "SEM VINCULO", "SEM RASTREADOR",
        "NAO VINCULADO", "NOT ACTIVE"
    )
    if any(token in texto_status for token in tokens_invalidos):
        return False

    return True


def buscar_posicoes_rastreadores(token_systemsat):
    url = "https://integration.systemsatx.com.br/Controlws/LastPosition/GetLastPositions"
    headers = {
        "Authorization": f"Bearer {token_systemsat}", "Content-Type": "application/json",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    resposta_http = requests.post(url, json={"ClientIntegrationCode": "55"}, headers=headers, timeout=30)
    resposta_http.raise_for_status()
    res = resposta_http.json()
    if not isinstance(res, list) or not res:
        raise ValueError("Resposta do SystemSat sem a lista de posições esperada.")
    rastreadores = {}
    for pos in res:
        if not rastreador_esta_vinculado_ssx(pos):
            continue

        placa_raw = pos.get("TrackedUnitIntegrationCode")
        data_str = pos.get("EventDate")
        if placa_raw and data_str:
            placa = limpar_placa(placa_raw)
            if not placa: continue
            empresa_sys = pos.get("ClientName") or pos.get("TrackedUnitDescription") or pos.get("GroupName") or "Desconhecida"
            dt_pos = parse_data_api(data_str, "EventDate")

            # Se por ventura tiver mais de um registro para a mesma placa sanitizada, manter o mais recente
            if placa in rastreadores:
                if dt_pos <= rastreadores[placa]["dt"]:
                    continue

            rastreadores[placa] = {
                "placa_original": placa_raw,
                "dt": dt_pos,
                "empresa_sys": normalizar_nome(empresa_sys)
            }
    return rastreadores

# =========================================================
# FUNÇÕES DE INTEGRAÇÃO ROTEIRIZAÇÃO
# =========================================================
def osrm_routing(pontos_lon_lat):
    all_geometry = []
    chunk_size = 24
    for i in range(0, len(pontos_lon_lat) - 1, chunk_size - 1):
        chunk = pontos_lon_lat[i:i+chunk_size]
        coords_str = ";".join([f"{lon},{lat}" for lon, lat in chunk])
        # Usando a API gratuita do OSRM para roteamento
        url = f"http://router.project-osrm.org/route/v1/driving/{coords_str}?overview=full&geometries=geojson"
        try:
            res = requests.get(url).json()
            if "routes" in res and res["routes"]:
                geom = res["routes"][0]["geometry"]["coordinates"]
                if i > 0 and all_geometry:
                    geom = geom[1:]
                all_geometry.extend(geom)
        except Exception as e:
            print(f"[OSRM] Erro na requisição: {e}")
    return all_geometry

def interpolar_rota(coordenadas_lon_lat, distancia_m=50):
    if not coordenadas_lon_lat: return []
    interpolados = [coordenadas_lon_lat[0]]
    dist_acumulada = 0.0

    for i in range(len(coordenadas_lon_lat) - 1):
        p1 = coordenadas_lon_lat[i]
        p2 = coordenadas_lon_lat[i+1]

        d = geodesic((p1[1], p1[0]), (p2[1], p2[0])).meters

        while dist_acumulada + d >= distancia_m:
            falta = distancia_m - dist_acumulada
            razao = falta / d if d > 0 else 0

            lon_interp = p1[0] + (p2[0] - p1[0]) * razao
            lat_interp = p1[1] + (p2[1] - p1[1]) * razao

            novo_ponto = [lon_interp, lat_interp]
            interpolados.append(novo_ponto)

            dist_acumulada = 0
            d -= falta
            p1 = novo_ponto

        dist_acumulada += d

    return interpolados

def buscar_pastas_linha(token):
    headers = {"content-type": "application/json", "authorization": f"Bearer {token}", "user-agent": "Mozilla/5.0"}
    payload = {
        "operationName": "BuscarPastas_Linha", "variables": {"filtros": {}, "page": 1},
        "query": "query BuscarPastas_Linha($filtros: FiltroBuscarPastasInput, $page: Float) { buscarPastas(filtros: $filtros, pageOptionsDto: {paginate: true, page: $page, take: 50}) { data { id numero descricao empresa { nomeFantasia razaoSocial } } } }"
    }
    res = requests.post("https://www.monitora.ms.gov.br/linha", json=payload, headers=headers, timeout=30).json()
    return res.get("data", {}).get("buscarPastas", {}).get("data", [])

def buscar_todos_pontos_monitora(token):
    headers = {"content-type": "application/json", "authorization": f"Bearer {token}", "user-agent": "Mozilla/5.0"}
    all_points = []
    has_next = True
    cursor = None
    while has_next:
        vars_payload = {"filter": "%%", "ativo": True}
        if cursor: vars_payload["after"] = cursor
        payload = {
            "operationName": "pontos", "variables": vars_payload,
            "query": "query pontos($filter: String, $after: ConnectionCursor, $ativo: Boolean) { pontos(filter: {or: [{nomeExibicao: {iLike: $filter}}, {cidade: {descricao: {iLike: $filter}}}], ativo: {is: $ativo}}, paging: {after: $after, first: 100}, sorting: {field: nome, direction: ASC}) { edges { node { id latitude longitude nome: nomeExibicao } } pageInfo { endCursor hasNextPage } } }"
        }
        res = requests.post("https://www.monitora.ms.gov.br/linha", json=payload, headers=headers, timeout=30).json()
        pontos_data = res.get("data", {}).get("pontos", {})
        for edge in pontos_data.get("edges", []):
            all_points.append(edge.get("node"))
        has_next = pontos_data.get("pageInfo", {}).get("hasNextPage", False)
        cursor = pontos_data.get("pageInfo", {}).get("endCursor")

    # Cria dicionario nome -> [lon, lat]
    dict_pontos = {}
    for p in all_points:
        if p and p.get("nome") and p.get("latitude") and p.get("longitude"):
            dict_pontos[p["nome"].strip()] = [float(p["longitude"]), float(p["latitude"])]
    return dict_pontos

def buscar_linha_trajeto(token, linha_id):
    headers = {"content-type": "application/json", "authorization": f"Bearer {token}", "user-agent": "Mozilla/5.0"}
    payload = {
        "operationName": "GET_LINHA", "variables": {"id": linha_id},
        "query": "query GET_LINHA($id: ID!) { linha(id: $id) { id nome numero sentidos { sentido trajetos(sorting: {field: ordem, direction: ASC}) { seccionamento { pontoInicial { nome } pontoFinal { nome } } } } } }"
    }
    res = requests.post("https://www.monitora.ms.gov.br/linha", json=payload, headers=headers, timeout=30).json()
    print(f"[Monitora] Linha trajeto resposta: {json.dumps(res, ensure_ascii=False)[:500]}")
    return res.get("data", {}).get("linha", {})

def enviar_rota_global(payload, token_sys, max_retries=3):
    url = "https://integration.systemsatx.com.br/GlobalBus/Route/InsertRouteWithTrajectory"
    headers = {"Authorization": f"Bearer {token_sys}", "Content-Type": "application/json"}

    for tentativa in range(max_retries):
        try:
            print(f"[SystemSat] Tentativa {tentativa + 1}/{max_retries} - Código: {payload.get('RouteIntegrationCode')}")
            res = requests.post(url, json=payload, headers=headers, timeout=120)

            print(f"[SystemSat] Status: {res.status_code} | Resposta: {res.text[:500]}")

            if res.status_code == 200:
                return res.json()

            # Se deu erro de duplicata, gera novo código e tenta novamente
            resp_text = res.text.lower()
            if "duplicate" in resp_text or "already exists" in resp_text or "duplicat" in resp_text:
                novo_code = str(uuid.uuid4())[:8]
                print(f"[SystemSat] RouteIntegrationCode duplicado! Gerando novo: {novo_code}")
                payload["RouteIntegrationCode"] = novo_code
                continue

            # Outro erro HTTP - levanta exceção com detalhes da resposta
            raise Exception(f"SystemSat retornou status {res.status_code}: {res.text[:500]}")

        except requests.exceptions.Timeout:
            print(f"[SystemSat] Timeout na tentativa {tentativa + 1}")
            if tentativa == max_retries - 1:
                raise Exception(f"Timeout após {max_retries} tentativas ao enviar rota para SystemSat")
            continue
        except requests.exceptions.ConnectionError as e:
            print(f"[SystemSat] Erro de conexão: {e}")
            raise Exception(f"Erro de conexão com SystemSat: {str(e)[:200]}")

    raise Exception("Falha ao enviar rota após todas as tentativas")

def processar_kml_file(file_content):
    root = ET.fromstring(file_content)
    namespaces = {'kml': 'http://www.opengis.net/kml/2.2'}
    coords_text = ""
    for ls in root.findall('.//kml:LineString/kml:coordinates', namespaces):
        coords_text = ls.text
        break
    if not coords_text:
        # Tenta sem namespace
        for ls in root.findall('.//LineString/coordinates'):
            coords_text = ls.text
            break

    pontos = []
    if coords_text:
        for par in coords_text.strip().split():
            partes = par.split(',')
            if len(partes) >= 2:
                pontos.append([float(partes[0]), float(partes[1])])
    return pontos

# =========================================================
# MOTOR DE CRUZAMENTO DE DADOS
# =========================================================
def gerar_relatorios_completos(token_agems, token_sys):
    global _report_status
    try:
        def log(msg): _report_status["log"].append(msg)

        log("🔍 Carregando dados da AGEMS e SystemSat...")
        veiculos = buscar_todos_veiculos(token_agems)
        empresas_regulares = buscar_empresas_regulares(token_agems)
        pedidos_desativacao = buscar_pedidos_desativacao(token_agems)
        rastreadores = buscar_posicoes_rastreadores(token_sys)

        data_hoje_dt = agora_utc()
        data_hoje_date = data_hoje_dt.date()

        # 1. Sincronização Geral (Garante que todos os veículos estejam na lista de Gestão)
        log("🔄 Sincronizando frota completa com o banco de dados...")
        with app.app_context():
            veiculos_db = {v.placa: v for v in Veiculo.query.all()}
            total_veiculos = len(veiculos)
            atualizacoes = []
            novos_veiculos = []
            mestre_snapshot = {}
            for indice, (placa, dados) in enumerate(veiculos.items(), start=1):
                v_mestre = veiculos_db.get(placa)
                if not v_mestre:
                    v_mestre = Veiculo(placa=placa)
                    veiculos_db[placa] = v_mestre
                    novos_veiculos.append(v_mestre)
                empresa = normalizar_nome(dados.get("empresa"))
                prefixo = dados.get("prefixo") or ""
                ultima_comunicacao = None
                if placa in rastreadores:
                    ultima_comunicacao = rastreadores[placa]["dt"].strftime("%d/%m/%Y %H:%M")
                dados_atualizacao = {"id": v_mestre.id}
                if v_mestre.empresa != empresa:
                    dados_atualizacao["empresa"] = empresa
                if v_mestre.prefixo != prefixo:
                    dados_atualizacao["prefixo"] = prefixo
                if ultima_comunicacao and v_mestre.ultima_comunicacao != ultima_comunicacao:
                    dados_atualizacao["ultima_comunicacao"] = ultima_comunicacao
                if len(dados_atualizacao) > 1 and v_mestre.id:
                    atualizacoes.append(dados_atualizacao)
                if not v_mestre.id:
                    v_mestre.empresa = empresa
                    v_mestre.prefixo = prefixo
                    if ultima_comunicacao:
                        v_mestre.ultima_comunicacao = ultima_comunicacao
                mestre_snapshot[placa] = {
                    "prefixo": prefixo,
                    "observacao": v_mestre.observacao or "",
                    "manutencao_manual": bool(v_mestre.manutencao_manual),
                    "ultima_comunicacao": ultima_comunicacao or v_mestre.ultima_comunicacao
                }
                if indice % 100 == 0 or indice == total_veiculos:
                    log(f"🔄 Frota sincronizada: {indice}/{total_veiculos}")
            log(f"💾 Alterações da frota: {len(atualizacoes)} atualizações e {len(novos_veiculos)} novos.")
            if atualizacoes:
                log("💾 Gravando alterações da frota em lote...")
                db.session.bulk_update_mappings(Veiculo, atualizacoes)
            if novos_veiculos:
                db.session.bulk_insert_mappings(Veiculo, [
                    {
                        "placa": veiculo.placa,
                        "empresa": veiculo.empresa,
                        "prefixo": veiculo.prefixo,
                        "ultima_comunicacao": rastreadores[veiculo.placa]["dt"].strftime("%d/%m/%Y %H:%M")
                        if veiculo.placa in rastreadores else None,
                        "manutencao_manual": False,
                        "precisa_manutencao": False
                    }
                    for veiculo in novos_veiculos
                ])
            db.session.commit()
            if atualizacoes or novos_veiculos:
                log("✅ Frota gravada no banco.")
            else:
                log("✅ Frota sem alterações no banco.")

        auditorias_pendentes = []

        def salvar_no_banco(placa, empresa, tipo, motivo="", offline=None, ultima="", prefixo=""):
            v_mestre = mestre_snapshot.get(placa, {})
            empresa_normalizada = normalizar_nome(empresa)
            prefixo_final = prefixo or v_mestre.get("prefixo", "")
            auditorias_pendentes.append(VeiculoAudit(
                placa=placa, prefixo=prefixo_final,
                empresa=empresa_normalizada, tipo_relatorio=tipo, motivo=motivo,
                observacao_mestre=v_mestre.get("observacao", ""), dias_offline=offline,
                ultima_posicao=ultima
            ))

        # 2. Execução da Auditoria (Regras 1, 2, 3)
        log("⚙️ Executando regras de auditoria...")

        # Estrutura para detecção de duplicatas
        placas_normalizadas = {} # normalizada: [placas_reais]

        qtd_ativos_vistoria_total = 0
        qtd_ativos_vistoria_com_rastreador = 0
        qtd_ativos_vistoria_sem_rastreador = 0

        lista_ativos_vistoria_total = []
        lista_ativos_vistoria_com_rastreador = []
        lista_ativos_vistoria_sem_rastreador = []

        for placa, dados in veiculos.items():
            dados, motivo_desat = selecionar_ocorrencia_veiculo(dados, pedidos_desativacao.get(placa))
            empresa = dados["empresa"]
            is_ativo = dados["ativo"]

            # Normalização para duplicatas
            p_norm = normalizar_placa_mercosul(placa)
            if p_norm not in placas_normalizadas:
                placas_normalizadas[p_norm] = []
            placas_normalizadas[p_norm].append(placa)

            has_vistoria = False
            if dados["vencimento_vistoria"]:
                try:
                    has_vistoria = data_vistoria_valida(dados["vencimento_vistoria"], data_hoje_date)
                except ValueError as exc:
                    log(str(exc))

            is_monitora_ok = is_ativo and has_vistoria
            is_empresa_regular = empresa in empresas_regulares
            is_desat_pendente = (motivo_desat is not None)
            is_inativo = (not is_ativo)
            ultima_comunicacao = rastreadores.get(placa, {}).get("dt")
            v_mestre = mestre_snapshot.get(placa, {})
            has_tracker = placa in rastreadores
            if has_tracker:
                ultima_comunicacao = rastreadores[placa]["dt"]
            else:
                ultima_comunicacao = None

            if is_monitora_ok and is_empresa_regular:
                qtd_ativos_vistoria_total += 1
                v_dados = {"Placa": placa, "Prefixo": dados.get("prefixo") or "", "Empresa": empresa}
                lista_ativos_vistoria_total.append(v_dados)
                if has_tracker:
                    qtd_ativos_vistoria_com_rastreador += 1
                    lista_ativos_vistoria_com_rastreador.append(v_dados)
                else:
                    qtd_ativos_vistoria_sem_rastreador += 1
                    lista_ativos_vistoria_sem_rastreador.append(v_dados)

            # A frota já foi carregada em memória na sincronização.
            precisa_manut_manual = v_mestre.get("manutencao_manual", False)

            # Regra: Desinstalação (Inativo ou pedido de desativação, mas ainda tem rastreador)
            if deve_entrar_desinstalacao(is_inativo, is_desat_pendente, is_empresa_regular, has_tracker):
                motivo = motivo_desat or (
                    "Empresa sem pasta com vencimento vigente"
                    if not is_empresa_regular
                    else "Inativo no sistema"
                )
                salvar_no_banco(placa, empresa, "desinstalacao", motivo, prefixo=dados.get("prefixo"))

            # Regra: Inativação Pendente sem Rastreador (Novo)
            elif is_desat_pendente and not has_tracker:
                salvar_no_banco(placa, empresa, "inativacao_pendente", f"Solicitação de inativação pendente e já sem rastreador. Motivo: {motivo_desat}", prefixo=dados.get("prefixo"))

            # Regra: Instalação (se não tiver flag manual de manutenção)
            elif is_monitora_ok and not has_tracker and is_empresa_regular and not precisa_manut_manual:
                salvar_no_banco(placa, empresa, "instalacao", "OK na AGEMS, mas sem rastreador", prefixo=dados.get("prefixo"))

            # Regra: Manutenção
            elif is_ativo and is_empresa_regular:
                if has_tracker:
                    dias_off = (data_hoje_dt - ultima_comunicacao).days
                    if deve_entrar_manutencao(dias_off, False):
                        if dias_off >= 15:
                            motivo = f"Offline há {dias_off} dias"
                        else:
                            motivo = f"Marcado para manutenção (Offline há {dias_off} dias)"
                        salvar_no_banco(placa, empresa, "manutencao", motivo, dias_off, ultima_comunicacao.strftime("%d/%m/%Y %H:%M"), prefixo=dados.get("prefixo"))

        # Regra: Placas Duplicadas
        # Sinalizar somente placa ativa na AGEMS com outra escrita no SystemSat.
        log("👯 Verificando placas duplicadas...")
        rastreadores_por_normalizada = {}
        for placa_sys in rastreadores:
            chave = normalizar_placa_mercosul(placa_sys)
            rastreadores_por_normalizada.setdefault(chave, []).append(placa_sys)
        for p_norm, lista_placas in placas_normalizadas.items():
            # Placa ativa na AGEMS vs outra escrita no Global (SystemSat).
            for p_agems in lista_placas:
                dados_v = veiculos.get(p_agems, {})
                if dados_v.get("ativo"): # Apenas se ativa na AGEMS
                    # A placa exata nos dois sistemas não é duplicata.
                    for p_global in rastreadores_por_normalizada.get(p_norm, []):
                        if p_global != p_agems:
                            salvar_no_banco(p_agems, dados_v.get("empresa"), "duplicadas", f"Conflito de formato: AGEMS ({p_agems}) vs Global ({p_global})", prefixo=dados_v.get("prefixo"))

        for placa_sys, info in rastreadores.items():
            if placa_sys not in veiculos:
                salvar_no_banco(placa_sys, info["empresa_sys"], "desinstalacao", "Rastreador órfão (não cadastrado na AGEMS)")

        with app.app_context():
            VeiculoAudit.query.delete()
            db.session.add_all(auditorias_pendentes)
            snapshot = db.session.get(RelatorioSnapshot, 1)
            if not snapshot:
                snapshot = RelatorioSnapshot(id=1)
                db.session.add(snapshot)
            snapshot.gerado_em = agora_utc()
            snapshot.ativos_vistoria_total = lista_ativos_vistoria_total
            snapshot.ativos_vistoria_com_rastreador = lista_ativos_vistoria_com_rastreador
            snapshot.ativos_vistoria_sem_rastreador = lista_ativos_vistoria_sem_rastreador
            db.session.commit()

        recriar_cache_relatorio(lista_ativos_vistoria_total, lista_ativos_vistoria_com_rastreador, lista_ativos_vistoria_sem_rastreador)
        _report_status["status"] = "done"
    except Exception as e:
        _report_status["status"] = "error"
        _report_status["error"] = str(e)

def recriar_cache_relatorio(lista_ativos_total=None, lista_ativos_com=None, lista_ativos_sem=None):
    with app.app_context():
        def get_grouped(tipo):
            items = VeiculoAudit.query.filter_by(tipo_relatorio=tipo).order_by(VeiculoAudit.empresa).all()
            return {emp: list(g) for emp, g in itertools.groupby(items, lambda x: x.empresa)}

        instalacao_grouped = get_grouped("instalacao")
        manutencao_grouped = get_grouped("manutencao")
        desinstalacao_grouped = get_grouped("desinstalacao")
        duplicadas_grouped = get_grouped("duplicadas")
        inativacao_pendente_grouped = get_grouped("inativacao_pendente")

        empresas_set = set(instalacao_grouped.keys()) | set(manutencao_grouped.keys()) | set(desinstalacao_grouped.keys())
        stats_por_empresa = {}
        for emp in empresas_set:
            stats_por_empresa[emp] = {
                "instalacao": len(instalacao_grouped.get(emp, [])),
                "manutencao": len(manutencao_grouped.get(emp, [])),
                "desinstalacao": len(desinstalacao_grouped.get(emp, []))
            }
        stats_por_empresa = dict(sorted(stats_por_empresa.items()))

        snapshot = db.session.get(RelatorioSnapshot, 1)

        with _report_lock:
            tot = lista_ativos_total if lista_ativos_total is not None else (
                snapshot.ativos_vistoria_total if snapshot else _report_cache.get("ativos_vistoria_total", [])
            )
            com = lista_ativos_com if lista_ativos_com is not None else (
                snapshot.ativos_vistoria_com_rastreador if snapshot else _report_cache.get("ativos_vistoria_com_rastreador", [])
            )
            sem = lista_ativos_sem if lista_ativos_sem is not None else (
                snapshot.ativos_vistoria_sem_rastreador if snapshot else _report_cache.get("ativos_vistoria_sem_rastreador", [])
            )
            gerado_em = (
                formatar_data_relatorio(snapshot.gerado_em)
                if snapshot else _report_cache.get("gerado_em", "")
            )

            _report_cache.clear()
            _report_cache.update({
                "instalacao": instalacao_grouped,
                "manutencao": manutencao_grouped,
                "desinstalacao": desinstalacao_grouped,
                "duplicadas": duplicadas_grouped,
                "inativacao_pendente": inativacao_pendente_grouped,
                "stats_por_empresa": stats_por_empresa,
                "ativos_vistoria_total": tot,
                "ativos_vistoria_com_rastreador": com,
                "ativos_vistoria_sem_rastreador": sem,
                "gerado_em": gerado_em,
                "stats": {
                    "instalacao": VeiculoAudit.query.filter_by(tipo_relatorio="instalacao").count(),
                    "manutencao": VeiculoAudit.query.filter_by(tipo_relatorio="manutencao").count(),
                    "desinstalacao": VeiculoAudit.query.filter_by(tipo_relatorio="desinstalacao").count(),
                    "duplicadas": VeiculoAudit.query.filter_by(tipo_relatorio="duplicadas").count(),
                    "inativacao_pendente": VeiculoAudit.query.filter_by(tipo_relatorio="inativacao_pendente").count(),
                    "ativos_vistoria_total": len(tot),
                    "ativos_vistoria_com_rastreador": len(com),
                    "ativos_vistoria_sem_rastreador": len(sem)
                }
            })

# =========================================================
# ROTAS FLASK
# =========================================================
@app.route("/")
def index():
    return redirect(url_for("dashboard")) if "logged_in" in session else redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        try:
            token = fazer_login_agems(request.form.get("email"), request.form.get("senha"))
            if token:
                session.update({"logged_in": True, "email": request.form.get("email"), "token_agems": token})
                return redirect(url_for("dashboard"))
            error = "Credenciais inválidas."
        except Exception as e: error = f"Erro: {str(e)}"
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
def dashboard():
    if "logged_in" not in session: return redirect(url_for("login"))
    with _report_lock: dados = dict(_report_cache)
    return render_template("dashboard.html", dados=dados, status=_report_status["status"], email=session.get("email"), log=_report_status["log"])

@app.route("/gerar", methods=["POST"])
def gerar():
    if "logged_in" not in session: return jsonify({"error": 401}), 401
    global _report_status
    _report_status = {"status": "running", "log": [], "error": None}

    # Captura o token FORA da thread para evitar o erro de context
    token_agems = session.get("token_agems")

    threading.Thread(target=executar_geracao, args=(token_agems,), daemon=True).start()
    return jsonify({"ok": True})

def executar_geracao(token_agems):
    try:
        gerar_relatorios_completos(token_agems, login_systemsat())
    except Exception as exc:
        _report_status["status"] = "error"
        _report_status["error"] = str(exc)

@app.route("/status")
def status():
    with _report_lock: dados = dict(_report_cache)
    return jsonify({ "status": _report_status["status"], "log": _report_status["log"], "error": _report_status["error"], "stats": dados.get("stats", {}) })

@app.route("/relatorio/<tipo>")
def relatorio(tipo):
    if "logged_in" not in session: return redirect(url_for("login"))
    with _report_lock: dados = dict(_report_cache)

    titulos = {
        "instalacao": "Relatório de Instalação",
        "manutencao": "Relatório de Manutenção",
        "desinstalacao": "Relatório de Desinstalação",
        "duplicadas": "Relatório de Placas Duplicadas (Mercosul)",
        "inativacao_pendente": "Inativação Pendente sem Rastreador"
    }

    return render_template("relatorio.html",
        tipo=tipo,
        titulo=titulos.get(tipo, f"Relatório de {tipo.capitalize()}"),
        itens=dados.get(tipo, {}),
        gerado_em=dados.get("gerado_em", ""),
        email=session.get("email")
    )

@app.route("/veiculos")
def veiculos():
    if "logged_in" not in session: return redirect(url_for("login"))
    search = request.args.get("search", "").strip().upper()
    query = Veiculo.query
    if search: query = query.filter((Veiculo.placa.like(f"%{search}%")) | (Veiculo.prefixo.like(f"%{search}%")) | (Veiculo.empresa.like(f"%{search}%")))
    return render_template("veiculos.html", veiculos=query.order_by(Veiculo.placa).all(), search=search)

@app.route("/veiculos/save", methods=["POST"])
def veiculos_save():
    placa = request.form.get("placa")
    v = Veiculo.query.filter_by(placa=placa).first()
    if v:
        v.observacao = request.form.get("observacao")
        pm_param = request.form.get("precisa_manutencao")
        novo_pm = (str(pm_param).lower() in ["true", "1", "on"])
        v.manutencao_manual = novo_pm
        db.session.commit()

        # Atualiza a auditoria de manutenção em tempo real no banco
        audit_manut = VeiculoAudit.query.filter_by(placa=placa, tipo_relatorio="manutencao").first()
        if novo_pm:
            if not audit_manut:
                # Remove do relatório de instalação caso estivesse lá
                VeiculoAudit.query.filter_by(placa=placa, tipo_relatorio="instalacao").delete()

                novo_audit = VeiculoAudit(
                    placa=v.placa,
                    prefixo=v.prefixo,
                    empresa=v.empresa,
                    tipo_relatorio="manutencao",
                    motivo="Marcado manualmente para manutenção",
                    observacao_mestre=v.observacao,
                    ultima_posicao=v.ultima_comunicacao
                )
                db.session.add(novo_audit)
            else:
                audit_manut.observacao_mestre = v.observacao
        else:
            if audit_manut and audit_manut.motivo.startswith("Marcado"):
                db.session.delete(audit_manut)

        db.session.commit()

        # Recria o cache global do relatório imediatamente
        recriar_cache_relatorio()

        return jsonify({"ok": True})
    return jsonify({"error": 404})

@app.route("/exportar/ativos")
def exportar_ativos():
    if "logged_in" not in session: return redirect(url_for("login"))
    with _report_lock:
        total = _report_cache.get("ativos_vistoria_total", [])
        com = _report_cache.get("ativos_vistoria_com_rastreador", [])
        sem = _report_cache.get("ativos_vistoria_sem_rastreador", [])

    if not total:
        return "Nenhum dado disponível. Gere um relatório primeiro.", 404

    df_total = pd.DataFrame(total)
    df_com = pd.DataFrame(com)
    df_sem = pd.DataFrame(sem)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df_total.to_excel(writer, index=False, sheet_name='Total')
        if not df_com.empty: df_com.to_excel(writer, index=False, sheet_name='Com Rastreador')
        if not df_sem.empty: df_sem.to_excel(writer, index=False, sheet_name='Sem Rastreador')

    output.seek(0)
    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = "attachment; filename=veiculos_ativos.xlsx"
    response.headers["Content-type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return response

@app.route("/exportar/relatorio/<tipo>")
def exportar_relatorio(tipo):
    if "logged_in" not in session: return redirect(url_for("login"))
    with _report_lock: dados = dict(_report_cache)

    itens = dados.get(tipo, {})
    if not itens:
        return "Nenhum dado disponível. Gere um relatório primeiro.", 404

    lista_plana = []
    for emp, lista in itens.items():
        for v in lista:
            row = {
                "Placa": v.placa,
                "Prefixo": v.prefixo or "",
                "Empresa": v.empresa,
                "Motivo/Detalhes": v.motivo or ""
            }
            if v.ultima_posicao: row["Última Posição"] = v.ultima_posicao
            if v.dias_offline is not None: row["Dias Offline"] = v.dias_offline
            lista_plana.append(row)

    df = pd.DataFrame(lista_plana)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name=tipo.capitalize())

    output.seek(0)
    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = f"attachment; filename=relatorio_{tipo}.xlsx"
    response.headers["Content-type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return response

@app.route("/whatsapp")
def whatsapp():
    if "logged_in" not in session: return redirect(url_for("login"))
    with _report_lock:
        instalacao = _report_cache.get("instalacao", {})
        manutencao = _report_cache.get("manutencao", {})
        desinstalacao = _report_cache.get("desinstalacao", {})

    empresas = set(instalacao.keys()) | set(manutencao.keys()) | set(desinstalacao.keys())
    empresas = sorted(list(empresas))

    mensagens = {}
    for emp in empresas:
        inst_list = [f"{v.placa} - {v.prefixo}" if v.prefixo else v.placa for v in instalacao.get(emp, [])]
        man_list = [f"{v.placa} - {v.prefixo}" if v.prefixo else v.placa for v in manutencao.get(emp, [])]
        des_list = [f"{v.placa} - {v.prefixo}" if v.prefixo else v.placa for v in desinstalacao.get(emp, [])]

        resp = EmpresaResponsavel.query.filter_by(empresa=emp).first()
        if resp and resp.titulo != 'Desconhecido' and resp.nome:
            saudacao = f"{resp.titulo}. {resp.nome}"
        elif resp and resp.nome:
            saudacao = f"{resp.nome}"
        else:
            saudacao = "senhor(a)"

        msg = f"Olá {saudacao}. Bom dia, sou o Danilo trabalho aqui na Track Land sou um dos responsáveis do contrato da AGEMS juntamente com o Vinicius.\n"

        if inst_list:
            msg += f"\nEstou entrando em contato para saber a disponibilidade dos veículos a seguir para a instalação:\n\n"
            msg += "\n".join(inst_list) + "\n"

        if man_list:
            msg += f"\ne também precisamos realizar as manutenções:\n\n"
            msg += "\n".join(man_list) + "\n"

        if des_list:
            msg += f"\ne também precisamos realizar as desinstalações:\n\n"
            msg += "\n".join(des_list) + "\n"

        msg += f"\nTem alguma data preferida pelo {saudacao}, eles viajam para Campo Grande alguma data?"

        mensagens[emp] = msg

    return render_template("whatsapp.html", empresas=empresas, mensagens=mensagens)

@app.route("/empresas")
def empresas_lista():
    if "logged_in" not in session: return redirect(url_for("login"))
    # Busca empresas regulares usando a API
    try:
        empresas_regulares = buscar_empresas_regulares(session.get("token_agems"))
    except Exception as e:
        print(f"Erro ao buscar empresas regulares: {e}")
        empresas_regulares = set()

    # Pegar todas as empresas distintas cadastradas nos veículos
    empresas_db = db.session.query(Veiculo.empresa).distinct().all()
    lista_empresas = [e[0] for e in empresas_db if e[0] and e[0] in empresas_regulares]
    lista_empresas.sort()

    responsaveis = EmpresaResponsavel.query.all()
    resp_map = {r.empresa: r for r in responsaveis}

    return render_template("empresas.html", empresas=lista_empresas, resp_map=resp_map)

@app.route("/empresas/save", methods=["POST"])
def empresas_save():
    if "logged_in" not in session: return jsonify({"error": 401}), 401
    empresa = request.form.get("empresa")
    titulo = request.form.get("titulo")
    nome = request.form.get("nome")

    resp = EmpresaResponsavel.query.filter_by(empresa=empresa).first()
    if not resp:
        resp = EmpresaResponsavel(empresa=empresa)
        db.session.add(resp)

    resp.titulo = titulo
    resp.nome = nome
    db.session.commit()
    return jsonify({"ok": True})

@app.route("/roteirizacao")
def roteirizacao():
    if "logged_in" not in session: return redirect(url_for("login"))
    return render_template("roteirizacao.html")

@app.route("/api/pastas")
def api_pastas():
    if "logged_in" not in session: return jsonify([]), 401
    pastas = buscar_pastas_linha(session.get("token_agems"))
    return jsonify(pastas)

@app.route("/api/linhas")
def api_linhas():
    if "logged_in" not in session: return jsonify([]), 401
    pasta_id = request.args.get("pasta_id")
    if not pasta_id: return jsonify([]), 400

    token = session.get("token_agems")
    headers = {"content-type": "application/json", "authorization": f"Bearer {token}", "user-agent": "Mozilla/5.0"}

    # Busca a pasta e extrai as linhas das Ordens de Serviço
    payload = {
        "operationName": "pasta",
        "variables": {"id": pasta_id},
        "query": "query pasta($id: ID!) { pasta(id: $id) { id ordemServicos { linhaSentido { sentido linha { id nome: numeroNome } } } } }"
    }
    try:
        res = requests.post("https://www.monitora.ms.gov.br/linha", json=payload, headers=headers, timeout=30).json()
        print(f"[Monitora] Pasta {pasta_id}: {json.dumps(res, ensure_ascii=False)[:800]}")

        pasta_data = res.get("data", {}).get("pasta", {})
        ordens = pasta_data.get("ordemServicos", [])

        # Extrair linhas únicas das ordens de serviço
        linhas_dict = {}
        for os_item in ordens:
            ls = os_item.get("linhaSentido", {})
            linha = ls.get("linha", {})
            if linha and linha.get("id") and linha["id"] not in linhas_dict:
                nome_completo = linha.get("nome", "")
                # nome vem como "411 - Glória de Dourados / Campo Grande"
                parts = nome_completo.split(" - ", 1)
                numero = parts[0].strip() if len(parts) > 1 else ""
                nome = parts[1].strip() if len(parts) > 1 else nome_completo
                linhas_dict[linha["id"]] = {
                    "id": linha["id"],
                    "numero": numero,
                    "nome": nome
                }

        linhas = list(linhas_dict.values())
        print(f"[Monitora] {len(linhas)} linha(s) encontrada(s) na pasta")
        if not linhas:
            return jsonify([]), 200
        return jsonify(linhas)
    except Exception as e:
        print(f"[Monitora] Erro ao buscar linhas da pasta: {e}")
        return jsonify([]), 500

@app.route("/api/gerar_rota", methods=["POST"])
def api_gerar_rota():
    if "logged_in" not in session: return jsonify({"error": "Não autenticado"}), 401
    data = request.json
    linha_id = data.get("linha_id")
    sentido_req = data.get("sentido", "IDA")
    distancia = int(data.get("distancia", 50))
    empresa_nome = data.get("empresa_nome", "25")

    token = session.get("token_agems")
    token_sys = login_systemsat()

    try:
        linha = buscar_linha_trajeto(token, linha_id)
        if not linha: return jsonify({"error": "Linha não encontrada"}), 404

        sentido_obj = None
        for s in linha.get("sentidos", []):
            if s.get("sentido") == sentido_req:
                sentido_obj = s
                break
        if not sentido_obj: return jsonify({"error": f"Sentido {sentido_req} não encontrado na linha"}), 404

        lista_nomes_pontos = []
        trajetos = sentido_obj.get("trajetos", [])
        if not trajetos: return jsonify({"error": "Nenhum trajeto encontrado"}), 404

        primeiro_secc = trajetos[0].get("seccionamento", {})
        lista_nomes_pontos.append(primeiro_secc.get("pontoInicial", {}).get("nome"))
        for t in trajetos:
            lista_nomes_pontos.append(t.get("seccionamento", {}).get("pontoFinal", {}).get("nome"))

        dict_pontos = buscar_todos_pontos_monitora(token)
        coordenadas_ordenadas = []
        for nome in lista_nomes_pontos:
            if not nome: continue
            nome_limpo = nome.strip()
            coord = dict_pontos.get(nome_limpo)
            if not coord:
                for kp, vp in dict_pontos.items():
                    if nome_limpo in kp or kp in nome_limpo:
                        coord = vp
                        break
            if coord:
                coordenadas_ordenadas.append(coord)

        if len(coordenadas_ordenadas) < 2:
            return jsonify({"error": "Não foi possível encontrar as coordenadas de pelo menos 2 pontos"}), 400

        geom_osrm = osrm_routing(coordenadas_ordenadas)
        if not geom_osrm:
            geom_osrm = coordenadas_ordenadas

        print(f"[Rota] Pontos OSRM: {len(geom_osrm)} | Coordenadas originais: {len(coordenadas_ordenadas)}")

        # Auto-ajuste de distância para ficar abaixo de 3000 pontos
        LIMITE_PONTOS = 3000
        distancias_possiveis = [200, 250, 300, 350]
        distancia_final = max(distancia, 200) # Garante que o mínimo seja 200 para testes
        geom_interpolada = interpolar_rota(geom_osrm, distancia_m=distancia_final)

        for d in distancias_possiveis:
            if d <= distancia_final:
                continue
            if len(geom_interpolada) <= LIMITE_PONTOS:
                break
            print(f"[Rota] {len(geom_interpolada)} pontos excede o limite de {LIMITE_PONTOS}. Aumentando distância de {distancia_final}m para {d}m...")
            distancia_final = d
            geom_interpolada = interpolar_rota(geom_osrm, distancia_m=distancia_final)

        if len(geom_interpolada) > LIMITE_PONTOS:
            print(f"[Rota] AVISO: Ainda com {len(geom_interpolada)} pontos mesmo a {distancia_final}m. Cortando em {LIMITE_PONTOS}.")
            geom_interpolada = geom_interpolada[:LIMITE_PONTOS]

        print(f"[Rota] Distância final: {distancia_final}m | Total pontos: {len(geom_interpolada)}")

        route_integration_code = str(uuid.uuid4())[:8]
        client_integration_code = "55"
        client_bus_code = empresa_nome

        points_payload = [{"Latitude": p[1], "Longitude": p[0]} for p in geom_interpolada]

        nome_rota = f"{linha.get('numero', '')} - {linha.get('nome', '')} - {sentido_req}"
        payload_global = {
            "RouteIntegrationCode": route_integration_code,
            "ClientIntegrationCode": client_integration_code,
            "ClientBusIntegrationCode": "86",
            "IdDirection": 1 if sentido_req == "IDA" else 2,
            "Tolerance": 50,
            "StartRadius": 100,
            "EndRadius": 100,
            "MinSpeed": 0,
            "MaxSpeed": 80,
            "Name": nome_rota,
            "Description": f"Gerado via Integração - Linha {linha.get('numero')}",
            "PanelWay": sentido_req,
            "RouteColor": "#3333CC",
            "Points": points_payload,
            "RouteType": 1,
            "Origin": "AGEMS",
            "MonitoringCancelTolerance": 9200,
            "TripCancelTolerance": 9200,
            "UpdateDepartureOnReenterPoint": True
        }

        # Log limpo sem a lista gigante de pontos
        payload_log = {k: v for k, v in payload_global.items() if k != 'Points'}
        print(f"[Rota] Payload (sem pontos): {payload_log}")
        print(f"[Rota] Qtd Pontos: {len(points_payload)} | Distância usada: {distancia_final}m")
        enviar_rota_global(payload_global, token_sys)
        return jsonify({"ok": True, "msg": f"Rota '{nome_rota}' com {len(points_payload)} pontos enviada! Cód: {route_integration_code}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/teste_rota", methods=["POST"])
def api_teste_rota():
    if "logged_in" not in session: return jsonify({"error": "Não autenticado"}), 401

    token_sys = login_systemsat()
    route_integration_code = str(uuid.uuid4())[:8]

    # Rota curta com 6 pontos para teste de timeout
    pontos_teste = [
        [-54.6122, -20.4697],
        [-54.6130, -20.4700],
        [-54.6140, -20.4710],
        [-54.6150, -20.4720],
        [-54.6160, -20.4730],
        [-54.6170, -20.4740]
    ]
    points_payload = [{"Latitude": p[1], "Longitude": p[0]} for p in pontos_teste]

    payload_global = {
        "RouteIntegrationCode": route_integration_code,
        "ClientIntegrationCode": "25",
        "ClientBusIntegrationCode": "25",
        "IdDirection": 1,
        "Tolerance": 50,
        "StartRadius": 100,
        "EndRadius": 100,
        "MinSpeed": 0,
        "MaxSpeed": 80,
        "Name": "ROTA TESTE 6 PONTOS",
        "Description": "Teste de envio de rota curta para validação",
        "PanelWay": "IDA",
        "RouteColor": "#3333CC",
        "Points": points_payload,
        "RouteType": 1,
        "Origin": "AGEMS",
        "MonitoringCancelTolerance": 9200,
        "TripCancelTolerance": 9200,
        "UpdateDepartureOnReenterPoint": True
    }

    try:
        enviar_rota_global(payload_global, token_sys)
        return jsonify({"ok": True, "msg": f"Rota teste com 6 pontos enviada! Cód: {route_integration_code}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/enviar_kml", methods=["POST"])
def api_enviar_kml():
    if "logged_in" not in session: return jsonify({"error": "Não autenticado"}), 401

    nome = request.form.get("nome")
    empresa = request.form.get("empresa")
    direction = int(request.form.get("direction", 1))
    distancia = int(request.form.get("distancia", 50))
    arquivo = request.files.get("arquivo")

    if not arquivo: return jsonify({"error": "Arquivo KML não enviado"}), 400

    try:
        conteudo_kml = arquivo.read().decode('utf-8')
        pontos_lon_lat = processar_kml_file(conteudo_kml)
        if not pontos_lon_lat:
            return jsonify({"error": "Nenhuma LineString encontrada"}), 400

        geom_interpolada = interpolar_rota(pontos_lon_lat, distancia_m=distancia)
        token_sys = login_systemsat()

        route_integration_code = str(uuid.uuid4())[:8]
        client_integration_code = "25"
        points_payload = [{"Latitude": p[1], "Longitude": p[0]} for p in geom_interpolada]

        payload_global = {
            "RouteIntegrationCode": route_integration_code,
            "ClientIntegrationCode": client_integration_code,
            "ClientBusIntegrationCode": empresa,
            "IdDirection": direction,
            "Tolerance": 50,
            "StartRadius": 100,
            "EndRadius": 100,
            "MinSpeed": 0,
            "MaxSpeed": 80,
            "Name": nome,
            "Description": "Gerado via KML",
            "PanelWay": "IDA" if direction == 1 else "VOLTA",
            "RouteColor": "#3333CC",
            "Points": points_payload,
            "RouteType": 0,
            "Origin": "AGEMS",
            "MonitoringCancelTolerance": 0,
            "TripCancelTolerance": 0,
            "UpdateDepartureOnReenterPoint": True
        }

        enviar_rota_global(payload_global, token_sys)
        return jsonify({"ok": True, "msg": f"KML '{nome}' com {len(points_payload)} pontos enviado! Cód: {route_integration_code}"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

with app.app_context():
    recriar_cache_relatorio()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=os.environ.get("FLASK_DEBUG") == "1")