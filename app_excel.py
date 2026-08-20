import requests
import re
import os
from datetime import datetime, timezone

# =========================================================
# FUNÇÕES DE UTILIDADE E LOGIN
# =========================================================
def normalizar_nome(nome):
    """Limpa espaços extras e padroniza maiúsculas para cruzar os dados perfeitamente"""
    if not nome: return ""
    return re.sub(r'\s+', ' ', nome).strip().upper()

def limpar_placa(placa):
    if not placa: return ""
    return re.sub(r'[^A-Z0-9]', '', str(placa).strip().upper())

def data_vistoria_valida(valor, hoje):
    if not valor: return False
    texto = str(valor).strip()
    try:
        vencimento = datetime.fromisoformat(texto.replace("Z", "+00:00")).date()
    except ValueError:
        vencimento = datetime.strptime(texto, "%d/%m/%Y").date()
    return vencimento >= hoje

def fazer_login():
    url_auth = "https://www.monitora.ms.gov.br/auth"
    email = os.environ.get("AGEMS_USERNAME")
    senha = os.environ.get("AGEMS_PASSWORD")
    if not email or not senha:
        raise RuntimeError("Defina AGEMS_USERNAME e AGEMS_PASSWORD.")
    payload = {
        "operationName": "Login_Auth",
        "variables": {"email": email, "senha": senha},
        "query": "mutation Login_Auth($email: String!, $senha: String!) {\n  login(input: {email: $email, senha: $senha}) {\n    token\n  }\n}"
    }
    headers = {"content-type": "application/json", "apollo-require-preflight": "true"}
    try:
        print("Autenticando na AGEMS...")
        res = requests.post(url_auth, json=payload, headers=headers)
        res.raise_for_status()
        return res.json().get('data', {}).get('login', {}).get('token')
    except Exception as e:
        print(f"Erro Login AGEMS: {e}")
        return None

def login_systemsat():
    hash_auth = os.environ.get("SYSTEMSAT_HASH_AUTH")
    username = os.environ.get("SYSTEMSAT_USERNAME")
    password = os.environ.get("SYSTEMSAT_PASSWORD")
    if not all((hash_auth, username, password)):
        raise RuntimeError("Defina SYSTEMSAT_HASH_AUTH, SYSTEMSAT_USERNAME e SYSTEMSAT_PASSWORD.")
    url = "https://integration.systemsatx.com.br/Login"
    try:
        print("Autenticando no SystemSat...")
        res = requests.post(url, params={"HashAuth": hash_auth, "Username": username, "Password": password})
        res.raise_for_status()
        return res.json().get("AccessToken")
    except Exception as e:
        print(f"Erro Login SystemSat: {e}")
        return None

# =========================================================
# FUNÇÕES DE EXTRAÇÃO DE DADOS
# =========================================================
def buscar_todos_veiculos(token):
    url_vistoria = "https://www.monitora.ms.gov.br/vistoria/"
    headers = {"content-type": "application/json", "authorization": f"Bearer {token}"}
    
    # 🚀 OTIMIZAÇÃO: Pedimos a dataVencimentoVistoria diretamente aqui
    payload = {
        "operationName": "BuscarRelatorioVeiculos",
        "variables": {"page": 1, "paginate": False, "nome": "%%", "placa": "%%", "status": ""},
        "query": """query BuscarRelatorioVeiculos($paginate: Boolean, $page: Float, $placa: String, $nome: String, $status: String, $ativo: Boolean, $dataCriacaoFim: String, $dataCriacaoInicio: String) {
          buscarRelatorioVeiculos(pageOptionsDto: {paginate: $paginate, page: $page}, filtros: {nome: $nome, placa: $placa, status: $status, numeroChassi: $placa, ativo: $ativo, dataCriacaoInicio: $dataCriacaoInicio, dataCriacaoFim: $dataCriacaoFim}) {
            data { placa, veiculoStatus, ativo, empresa, dataVencimentoVistoria }
          }
        }"""
    }

    print("Baixando dicionário mestre de veículos (AGEMS)...")
    res = requests.post(url_vistoria, json=payload, headers=headers)
    lista_veiculos = res.json().get("data", {}).get("buscarRelatorioVeiculos", {}).get("data", [])
    
    veiculos_mestre = {}
    for v in lista_veiculos:
        placa_raw = v.get("placa")
        if not placa_raw: continue
        placa = limpar_placa(placa_raw)
        if not placa: continue
        veiculos_mestre[placa] = {
            "ativo": v.get("ativo"),
            "status": v.get("veiculoStatus"),
            "empresa": normalizar_nome(v.get("empresa", "")),
            "vencimento_vistoria": v.get("dataVencimentoVistoria")
        }
    return veiculos_mestre

def buscar_empresas_regulares(token):
    url_req = "https://www.monitora.ms.gov.br/req/"
    headers = {"content-type": "application/json", "authorization": f"Bearer {token}"}
    payload = {
        "operationName": "buscarEmpresasSemPaginacao",
        "variables": {},
        "query": "query buscarEmpresasSemPaginacao { buscarEmpresasSemPaginacao { pessoaJuridica { razaoSocial } empresaStatus { descricao } } }"
    }

    print("Verificando status de regularidade das transportadoras...")
    res = requests.post(url_req, json=payload, headers=headers)
    lista_empresas = res.json().get("data", {}).get("buscarEmpresasSemPaginacao", [])
    
    empresas_regulares = set()
    for emp in lista_empresas:
        if emp.get("empresaStatus", {}).get("descricao", "").upper() == "REGULAR":
            razao_social = normalizar_nome(emp.get("pessoaJuridica", {}).get("razaoSocial"))
            if razao_social: empresas_regulares.add(razao_social)
    return empresas_regulares

def buscar_pedidos_desativacao(token):
    url_vistoria = "https://www.monitora.ms.gov.br/vistoria/"
    headers = {"content-type": "application/json", "authorization": f"Bearer {token}"}
    query = """query solicitacoesAtivacoesDesativacoesVeiculos_vistoria($veiculo: String, $isNull: StringFieldComparison, $tipoSolicitacao: TipoSolicitacaoEnum, $aprovado: Boolean, $paging: CursorPaging) {
      solicitacoesAtivacoesDesativacoesVeiculos(filter: {veiculo: {or: [{placa: {iLike: $veiculo}}, {numeroChassi: {iLike: $veiculo}}, {prefixo: {iLike: $veiculo}}]}, quemAnalisouId: $isNull, tipoSolicitacao: {like: $tipoSolicitacao}, aprovado: {is: $aprovado}}, paging: $paging, sorting: {field: createdAt, direction: DESC}) {
        edges { node { motivo, tipoSolicitacao, veiculo { placa } } }
        pageInfo { endCursor, hasNextPage }
      }
    }"""

    tem_proxima = True
    cursor_atual = None
    historico = {}

    print("Lendo timeline de desativações (Isso pode levar alguns segundos)...")
    while tem_proxima:
        paging_param = {"first": 50}
        if cursor_atual: paging_param["after"] = cursor_atual
        payload = {"operationName": "solicitacoesAtivacoesDesativacoesVeiculos_vistoria", "variables": {"paging": paging_param, "veiculo": "%%", "isNull": {}}, "query": query}
        
        res = requests.post(url_vistoria, json=payload, headers=headers).json()
        solic_node = res.get("data", {}).get("solicitacoesAtivacoesDesativacoesVeiculos", {})
        
        for edge in solic_node.get("edges", []):
            node = edge.get("node", {})
            placa_raw = node.get("veiculo", {}).get("placa")
            if not placa_raw: continue
            placa = limpar_placa(placa_raw)
            if placa and placa not in historico:
                historico[placa] = {"tipo": node.get("tipoSolicitacao"), "motivo": node.get("motivo", "Sem motivo")}
        
        page_info = solic_node.get("pageInfo", {})
        tem_proxima = page_info.get("hasNextPage", False)
        cursor_atual = page_info.get("endCursor")

    return {p: v["motivo"] for p, v in historico.items() if v["tipo"] == "DESATIVACAO"}

def buscar_posicoes_rastreadores(token_systemsat):
    url = "https://integration.systemsatx.com.br/Controlws/LastPosition/GetLastPositions"
    headers = {"Authorization": f"Bearer {token_systemsat}", "Content-Type": "application/json"}
    
    print("Baixando dados de telemetria SystemSat...")
    res = requests.post(url, json={"ClientIntegrationCode": "55"}, headers=headers).json()
    
    rastreadores = {}
    for pos in res:
        placa_raw = pos.get("TrackedUnitIntegrationCode")
        data_str = pos.get("EventDate")
        if placa_raw and data_str:
            placa = limpar_placa(placa_raw)
            if not placa: continue
            dt_pos = datetime.fromisoformat(data_str.replace("Z", "+00:00"))
            if placa in rastreadores and dt_pos <= rastreadores[placa]:
                continue
            rastreadores[placa] = dt_pos
    return rastreadores

# =========================================================
# O GRANDE CRUZAMENTO DE DADOS (MOTOR)
# =========================================================
if __name__ == "__main__":
    t_agems = fazer_login()
    t_sys = login_systemsat()
    
    if t_agems and t_sys:
        veiculos = buscar_todos_veiculos(t_agems)
        empresas_regulares = buscar_empresas_regulares(t_agems)
        pedidos_desativacao = buscar_pedidos_desativacao(t_agems)
        rastreadores = buscar_posicoes_rastreadores(t_sys)
        
        rel_instalacao = []
        rel_manutencao = []
        rel_desinstalacao = []
        
        data_hoje_dt = datetime.now(timezone.utc)
        data_hoje_date = data_hoje_dt.date()
        
        print("\nCruzando inteligência de dados...\n")
        
        for placa, dados in veiculos.items():
            empresa = dados["empresa"]
            is_ativo = dados["ativo"]
            is_aprovado = dados["status"] in ["aprovado", "pendente envio"]
            
            # Valida Vistoria pela Data embutida
            has_vistoria = False
            if dados["vencimento_vistoria"]:
                try:
                    has_vistoria = data_vistoria_valida(dados["vencimento_vistoria"], data_hoje_date)
                except ValueError as exc:
                    raise ValueError(f"Data de vistoria inválida para {placa}: {dados['vencimento_vistoria']!r}") from exc
                
            is_monitora_ok = is_ativo and is_aprovado and has_vistoria
            is_empresa_regular = empresa in empresas_regulares
            
            motivo_desativacao = pedidos_desativacao.get(placa)
            is_desativado_ou_inativo = (not is_ativo) or (motivo_desativacao is not None)
            
            has_tracker = placa in rastreadores
            base = {"placa": placa, "empresa": empresa, "observacao": ""}

            # REGRA 1: DESINSTALAÇÃO
            if is_desativado_ou_inativo and has_tracker:
                base["motivo"] = motivo_desativacao if motivo_desativacao else "Inativo no sistema central"
                rel_desinstalacao.append(base)
                continue 

            # REGRA 2: INSTALAÇÃO
            if is_monitora_ok and not has_tracker and not is_desativado_ou_inativo:
                if is_empresa_regular:
                    rel_instalacao.append(base)
                continue

            # REGRA 3: MANUTENÇÃO
            if is_monitora_ok and has_tracker and not is_desativado_ou_inativo:
                dias_offline = (data_hoje_dt - rastreadores[placa]).days
                if dias_offline >= 15:
                    base["dias_offline"] = dias_offline
                    base["ultima_posicao"] = rastreadores[placa].strftime("%d/%m/%Y %H:%M")
                    rel_manutencao.append(base)

        print("="*70)
        print("📊 DASHBOARD DE OPERAÇÕES TRACK LAND / AGEMS")
        print("="*70)
        print(f"🛠️  VEÍCULOS PARA INSTALAÇÃO (Empresa Regular, Sem rastreador): {len(rel_instalacao)}")
        print(f"⚠️  VEÍCULOS PARA MANUTENÇÃO (OK mas >15 dias offline): {len(rel_manutencao)}")
        print(f"🛑 VEÍCULOS PARA DESINSTALAÇÃO (Inativos/Desativados com rastreador): {len(rel_desinstalacao)}")
        print("="*70)