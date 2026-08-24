import requests
import re
import os
from datetime import datetime, timezone, date
import pandas as pd

# =========================================================
# FUNÇÕES DE UTILIDADE E LOGIN
# =========================================================
def limpar_placa(placa):
    if not placa: return ""
    return re.sub(r'[^A-Z0-9]', '', str(placa).strip().upper())

def padronizar_mercosul(placa):
    """Converte virtualmente a placa para padrão Mercosul apenas para o Alerta"""
    placa = limpar_placa(placa)
    if len(placa) == 7 and placa[:3].isalpha() and placa[3:].isdigit():
        conversao = {'0':'A', '1':'B', '2':'C', '3':'D', '4':'E', 
                     '5':'F', '6':'G', '7':'H', '8':'I', '9':'J'}
        return placa[:4] + conversao[placa[4]] + placa[5:]
    return placa

def normalizar_nome(nome):
    if not nome: return ""
    return re.sub(r'\s+', ' ', str(nome)).strip().upper()

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
            
        is_ativo_novo = v.get("ativo", False)
        
        # Blindagem: Se o veículo já estiver salvo como ativo, não sobrescreve com inativo
        if placa in veiculos_mestre:
            if veiculos_mestre[placa]["ativo"] is True and is_ativo_novo is False:
                continue 
                
        veiculos_mestre[placa] = {
            "ativo": is_ativo_novo,
            "status": v.get("veiculoStatus"),
            "empresa": normalizar_nome(v.get("empresa", "")),
            "vencimento_vistoria": v.get("dataVencimentoVistoria")
        }
    return veiculos_mestre

def buscar_empresas_com_pasta_valida(token):
    headers = {"content-type": "application/json", "authorization": f"Bearer {token}"}
    url = "https://www.monitora.ms.gov.br/linha"
    empresas_com_pasta = set()
    page = 1
    has_more = True
    data_hoje = date.today()
    
    print("Verificando empresas com PASTA válida na AGEMS...")
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
            print(f"Erro ao buscar pastas: {e}")
            break
            
    return empresas_com_pasta

def buscar_empresas_regulares(token):
    return buscar_empresas_com_pasta_valida(token)

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
    print("Lendo timeline de desativações...")
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
# MOTOR E GERAÇÃO DO EXCEL
# =========================================================
if __name__ == "__main__":
    t_agems = fazer_login()
    t_sys = login_systemsat()
    
    if t_agems and t_sys:
        veiculos = buscar_todos_veiculos(t_agems)
        empresas_com_pasta = buscar_empresas_com_pasta_valida(t_agems)
        pedidos_desativacao = buscar_pedidos_desativacao(t_agems)
        rastreadores = buscar_posicoes_rastreadores(t_sys)
        
        # 🚨 NOVO: Dicionário invisível só para gerar o aviso Mercosul no final
        sys_mercosul_map = {}
        for p_sys in rastreadores.keys():
            sys_mercosul_map[padronizar_mercosul(p_sys)] = p_sys
        
        data_hoje_dt = datetime.now(timezone.utc)
        data_hoje_date = data_hoje_dt.date()
        
        print("\nCruzando dados e montando planilha de auditoria...\n")
        
        linhas_excel = []
        avisos_mercosul = [] # Lista de placas para você corrigir
        
        for placa, dados in veiculos.items():
            empresa = dados["empresa"]
            is_ativo = dados["ativo"]
            is_aprovado = dados["status"] in ["aprovado", "pendente envio"]
            
            # Valida Vistoria 
            has_vistoria = False
            if dados["vencimento_vistoria"]:
                try:
                    has_vistoria = data_vistoria_valida(dados["vencimento_vistoria"], data_hoje_date)
                except ValueError as exc:
                    raise ValueError(f"Data de vistoria inválida para {placa}: {dados['vencimento_vistoria']!r}") from exc
                
            is_monitora_ok = is_ativo and is_aprovado and has_vistoria
            is_empresa_pasta_ok = empresa in empresas_com_pasta
            
            motivo_desativacao = pedidos_desativacao.get(placa)
            is_desativado_ou_inativo = (not is_ativo) or (motivo_desativacao is not None)
            
            # 🚨 MANTIDO CRUZAMENTO ESTRITO (Garante que os números fiquem perfeitos)
            has_tracker = placa in rastreadores
            
            # 🚨 GERAÇÃO DO AVISO MERCOSUL
            if not has_tracker:
                placa_mercosul = padronizar_mercosul(placa)
                # Se não achou exato, mas a versão mercosul existe lá no System Sat:
                if placa_mercosul in sys_mercosul_map:
                    placa_sys_errada = sys_mercosul_map[placa_mercosul]
                    avisos_mercosul.append(f"   -> Placa na AGEMS: {placa} | Cadastrada no SystemSat como: {placa_sys_errada}")
            
            marca_monitora = "X" if is_monitora_ok else ""
            marca_systemsat = "X" if has_tracker else ""
            
            justificativa = ""
            if is_desativado_ou_inativo and has_tracker:
                justificativa = f"DESINSTALAÇÃO: {motivo_desativacao if motivo_desativacao else 'Inativo no sistema AGEMS'}"
            elif is_monitora_ok and is_empresa_pasta_ok and not has_tracker:
                justificativa = "INSTALAÇÃO: OK na AGEMS e empresa com pasta ativa, mas sem rastreador"
            elif is_monitora_ok and has_tracker and not is_desativado_ou_inativo:
                dias_offline = (data_hoje_dt - rastreadores[placa]).days
                if dias_offline >= 15:
                    justificativa = f"MANUTENÇÃO: Offline há {dias_offline} dias"
                else:
                    justificativa = "SITUAÇÃO OK: Veículo com rastreador comunicando."
            elif is_monitora_ok and not is_empresa_pasta_ok:
                justificativa = "IGNORADO - Empresa sem pasta ativa na AGEMS"
            else:
                justificativa = "IGNORADO - Inativo ou faltando documento/vistoria, sem rastreador."

            linhas_excel.append({
                "PLACA": placa,
                "EMPRESA": empresa,
                "MONITORA (X)": marca_monitora,
                "SYSTEM SAT (X)": marca_systemsat,
                "MOTIVO / JUSTIFICATIVA": justificativa
            })

        for placa in rastreadores:
            if placa not in veiculos:
                linhas_excel.append({
                    "PLACA": placa,
                    "EMPRESA": "DESCONHECIDA",
                    "MONITORA (X)": "",
                    "SYSTEM SAT (X)": "X",
                    "MOTIVO / JUSTIFICATIVA": "DESINSTALAÇÃO: Rastreador existe mas placa foi excluída da AGEMS."
                })

        # =========================================================
        # GERAÇÃO DO ARQUIVO EXCEL E DASHBOARD
        # =========================================================
        df_principal = pd.DataFrame(linhas_excel)
        
        qtd_instalacao = sum(1 for linha in linhas_excel if linha["MOTIVO / JUSTIFICATIVA"].startswith("INSTALAÇÃO"))
        qtd_manutencao = sum(1 for linha in linhas_excel if linha["MOTIVO / JUSTIFICATIVA"].startswith("MANUTENÇÃO"))
        qtd_desinstalacao = sum(1 for linha in linhas_excel if linha["MOTIVO / JUSTIFICATIVA"].startswith("DESINSTALAÇÃO"))
        qtd_ok = sum(1 for linha in linhas_excel if linha["MOTIVO / JUSTIFICATIVA"].startswith("SITUAÇÃO OK"))
        qtd_ignorado = sum(1 for linha in linhas_excel if linha["MOTIVO / JUSTIFICATIVA"].startswith("IGNORADO"))

        df_resumo = pd.DataFrame([
            {"STATUS DA FROTA": "🛠️ VEÍCULOS PARA INSTALAÇÃO", "QUANTIDADE": qtd_instalacao},
            {"STATUS DA FROTA": "⚠️ VEÍCULOS PARA MANUTENÇÃO", "QUANTIDADE": qtd_manutencao},
            {"STATUS DA FROTA": "🛑 VEÍCULOS PARA DESINSTALAÇÃO", "QUANTIDADE": qtd_desinstalacao},
            {"STATUS DA FROTA": "✅ VEÍCULOS OK (COMUNICANDO)", "QUANTIDADE": qtd_ok},
            {"STATUS DA FROTA": "⚪ VEÍCULOS IGNORADOS (IRREGULARES/INATIVOS)", "QUANTIDADE": qtd_ignorado}
        ])

        nome_arquivo = "Auditoria_Frota_Completa.xlsx"
        
        with pd.ExcelWriter(nome_arquivo, engine='openpyxl') as writer:
            df_resumo.to_excel(writer, index=False, sheet_name="Resumo (Dashboard)")
            df_principal.to_excel(writer, index=False, sheet_name="Auditoria Completa")
            
            worksheet_resumo = writer.sheets["Resumo (Dashboard)"]
            worksheet_resumo.column_dimensions['A'].width = 55
            worksheet_resumo.column_dimensions['B'].width = 15
            
            worksheet_audit = writer.sheets["Auditoria Completa"]
            worksheet_audit.column_dimensions['A'].width = 12
            worksheet_audit.column_dimensions['B'].width = 45
            worksheet_audit.column_dimensions['C'].width = 15
            worksheet_audit.column_dimensions['D'].width = 15
            worksheet_audit.column_dimensions['E'].width = 80
        
        print("="*70)
        print("📊 DASHBOARD DE OPERAÇÕES TRACK LAND / AGEMS")
        print("="*70)
        print(f"🛠️  VEÍCULOS PARA INSTALAÇÃO: {qtd_instalacao}")
        print(f"⚠️  VEÍCULOS PARA MANUTENÇÃO: {qtd_manutencao}")
        print(f"🛑 VEÍCULOS PARA DESINSTALAÇÃO: {qtd_desinstalacao}")
        print(f"✅ VEÍCULOS OK (COMUNICANDO): {qtd_ok}")
        print(f"⚪ VEÍCULOS IGNORADOS: {qtd_ignorado}")
        print("="*70)
        
        # O AVISO MERCOSUL APARECE AQUI!
        if avisos_mercosul:
            print("\n⚠️ AVISO: DIVERGÊNCIA DE PADRÃO MERCOSUL DETECTADA!")
            print("Os veículos abaixo CAÍRAM EM INSTALAÇÃO, mas o rastreador")
            print("está cadastrado com outro padrão de placa na SystemSat.")
            print("Atualize na SystemSat para corrigir o sistema:\n")
            for aviso in set(avisos_mercosul): # Usei set para não repetir o print
                print(aviso)
            print("-" * 70)
            
        print(f"\n✅ EXCEL GERADO COM SUCESSO: '{nome_arquivo}'")