import requests
import pandas as pd
import time
import os

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

def obter_query_graphql():
    # Retorna exatamente a estrutura da query enviada na sua requisição original
    return """query EmpresasPessoaJuridicas_Req($nomeOrCnpj: String, $status: String, $ativo: Boolean, $vencido: Boolean, $after: ConnectionCursor, $tipoEmpresaId: ID) {
  empresas(
    filter: {empresaStatusId: {eq: $status}, ativo: {is: $ativo}, vencido: {is: $vencido}, tiposEmpresas: {id: {eq: $tipoEmpresaId}}, pessoaJuridica: {or: [{cnpj: {iLike: $nomeOrCnpj}}, {razaoSocial: {iLike: $nomeOrCnpj}}]}}
    paging: {after: $after, first: 20}
    sorting: {field: razaoSocial, direction: ASC}
  ) {
    totalCount
    edges {
      cursor
      node {
        id
        codigo
        vencido
        ativo
        empresaStatus {
          descricao
        }
        responsavel {
          pessoaFisica {
            pessoa {
              nome
            }
          }
        }
        tiposEmpresas {
          descricao
        }
        enderecos {
          nodes {
            cidade {
              descricao
              estado {
                descricao
              }
            }
          }
        }
        telefones {
          nodes {
            ddd
            numero
          }
        }
        usuarios {
          email
          nome
        }
        pessoaJuridica {
          razaoSocial
          cnpj
        }
      }
    }
    pageInfo {
      endCursor
      hasNextPage
    }
  }
}"""

def extrair_dados_empresa(node):
    """Mapeia com segurança os dados de cada nó da árvore JSON"""
    pj = node.get("pessoaJuridica") or {}
    razao_social = pj.get("razaoSocial", "Não Informado")
    cnpj = pj.get("cnpj", "Não Informado")
    
    # Responsável
    responsavel = "Não Informado"
    resp_obj = node.get("responsavel")
    if resp_obj and resp_obj.get("pessoaFisica"):
        pf = resp_obj["pessoaFisica"].get("pessoa")
        if pf:
            responsavel = pf.get("nome", "Não Informado")
            
    # Telefone / Contato (Pega o primeiro da lista se houver)
    telefone = "Não Informado"
    tel_obj = node.get("telefones", {})
    if tel_obj and tel_obj.get("nodes"):
        nodes = tel_obj["nodes"]
        if len(nodes) > 0:
            ddd = nodes[0].get("ddd", "")
            num = nodes[0].get("numero", "")
            telefone = f"({ddd}) {num}" if ddd else num
            
    # Cidade e Estado
    cidade = "Não Informada"
    estado = "Não Informado"
    end_obj = node.get("enderecos", {})
    if end_obj and end_obj.get("nodes"):
        nodes = end_obj["nodes"]
        if len(nodes) > 0:
            cid_obj = nodes[0].get("cidade")
            if cid_obj:
                cidade = cid_obj.get("descricao", "Não Informada")
                est_obj = cid_obj.get("estado")
                if est_obj:
                    estado = est_obj.get("descricao", "Não Informado")
                    
    # Status e Vencimento
    status = node.get("empresaStatus", {}).get("descricao", "Ativo" if node.get("ativo") else "Inativo")
    vencido = "Sim" if node.get("vencido") else "Não"
    
    # Concatena múltiplos tipos de serviço ou usuários vinculados (se houver)
    tipos = ", ".join([t.get("descricao", "") for t in node.get("tiposEmpresas", []) if t.get("descricao")])
    emails = ", ".join([u.get("email", "") for u in node.get("usuarios", []) if u.get("email")])

    return {
        "Código": node.get("codigo"),
        "Razão Social": razao_social,
        "CNPJ": cnpj,
        "Responsável": responsavel,
        "Contato / Telefone": telefone,
        "Cidade": cidade,
        "Estado": estado,
        "Status Cadastral": status,
        "Vencido": vencido,
        "Tipos de Serviço": tipos,
        "Emails Vinculados": emails
    }

def coletar_todas_empresas(email, senha):
    print("[*] Iniciando autenticação para capturar Token dinâmico...")
    try:
        token = fazer_login_agems(email, senha)
        print("[+] Autenticado com sucesso!")
    except Exception as e:
        print(f"[-] Erro crítico na autenticação: {e}")
        return

    url_req = "https://www.monitora.ms.gov.br/req/"
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9,pt-BR;q=0.8,pt;q=0.7",
        "apollo-require-preflight": "true",
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    lista_empresas = []
    tem_proxima_pagina = True
    cursor_atual = None
    pagina = 1

    print("[*] Iniciando a paginação e coleta na Agems...")
    
    while tem_proxima_pagina:
        print(f"    -> Extraindo dados da página {pagina}...")
        
        payload = {
            "operationName": "EmpresasPessoaJuridicas_Req",
            "variables": {
                "nomeOrCnpj": "%%",
                "after": cursor_atual
            },
            "query": obter_query_graphql()
        }
        
        try:
            res = requests.post(url_req, json=payload, headers=headers, timeout=20)
            res.raise_for_status()
            dados_json = res.json()
            
            empresas_data = dados_json.get("data", {}).get("empresas", {})
            edges = empresas_data.get("edges", [])
            page_info = empresas_data.get("pageInfo", {})
            
            for edge in edges:
                node = edge.get("node")
                if node:
                    lista_empresas.append(extrair_dados_empresa(node))
            
            # Condições de controle do Loop baseadas na API
            tem_proxima_pagina = page_info.get("hasNextPage", False)
            cursor_atual = page_info.get("endCursor")
            
            if not cursor_atual:
                break
                
            pagina += 1
            time.sleep(0.4) # Pequena pausa de segurança entre requisições
            
        except Exception as e:
            print(f"[-] Falha na requisição da página {pagina}: {e}")
            break

    if lista_empresas:
        print(f"[+] Total de {len(lista_empresas)} registros capturados.")
        df = pd.DataFrame(lista_empresas)
        
        nome_arquivo = "empresas_cadastradas.xlsx"
        
        # Salva formatando o tamanho ideal de largura de coluna automaticamente
        with pd.ExcelWriter(nome_arquivo, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Empresas Coletadas')
            
            worksheet = writer.sheets['Empresas Coletadas']
            for col in worksheet.columns:
                max_len = max(len(str(cell.value or '')) for cell in col)
                col_letter = col[0].column_letter
                worksheet.column_dimensions[col_letter].width = max(max_len + 3, 12)
                
        print(f"[+] Concluído! Planilha gerada com sucesso: '{nome_arquivo}'")
    else:
        print("[-] Nenhum registro encontrado para exportação.")

if __name__ == "__main__":
  email = os.environ.get("AGEMS_USERNAME")
  senha = os.environ.get("AGEMS_PASSWORD")
  if not email or not senha:
    raise RuntimeError("Defina AGEMS_USERNAME e AGEMS_PASSWORD.")
  coletar_todas_empresas(email, senha)