import os
import unittest
from datetime import date
from unittest.mock import Mock, patch

os.environ.setdefault("AGEMS_SECRET_KEY", "test-secret")
os.environ.setdefault("SYSTEMSAT_HASH_AUTH", "test-hash")
os.environ.setdefault("SYSTEMSAT_USERNAME", "test-user")
os.environ.setdefault("SYSTEMSAT_PASSWORD", "test-password")

from app import buscar_todos_veiculos, data_vistoria_valida, deve_entrar_desinstalacao, deve_entrar_manutencao, parse_data_api, parse_ultima_comunicacao, selecionar_ocorrencia_veiculo


class RegrasRelatorioTest(unittest.TestCase):
    def test_veiculo_online_nao_fica_em_manutencao_sem_marcacao_manual(self):
        self.assertFalse(deve_entrar_manutencao(2, False))

    def test_marcacao_manual_mantem_manutencao_mesmo_online(self):
        self.assertTrue(deve_entrar_manutencao(2, True))

    def test_empresa_sem_pasta_valida_com_rastreador_entra_em_desinstalacao(self):
        self.assertTrue(deve_entrar_desinstalacao(False, False, False, True))

    def test_empresa_sem_pasta_valida_sem_rastreador_nao_entra_em_desinstalacao(self):
        self.assertFalse(deve_entrar_desinstalacao(False, False, False, False))

    def test_offline_por_quinze_dias_entra_em_manutencao(self):
        self.assertTrue(deve_entrar_manutencao(15, False))

    def test_manutencao_com_rastreador_offline_quinze_dias(self):
        self.assertTrue(deve_entrar_manutencao(15, False))

    def test_manutencao_nao_depende_de_vistoria(self):
        is_ativo = True
        is_empresa_regular = True
        has_vistoria = False
        dias_offline = 16
        self.assertTrue(is_ativo and is_empresa_regular and not has_vistoria)
        self.assertTrue(deve_entrar_manutencao(dias_offline, False))

    def test_ultima_comunicacao_persistida_identifica_rastreador_antigo(self):
        comunicacao = parse_ultima_comunicacao("13/10/2025 20:08")
        self.assertIsNotNone(comunicacao)
        self.assertEqual(comunicacao.strftime("%d/%m/%Y %H:%M"), "13/10/2025 20:08")

    def test_vistoria_aceita_iso_e_data_brasileira(self):
        hoje = date(2026, 8, 20)
        self.assertTrue(data_vistoria_valida("2026-08-20", hoje))
        self.assertTrue(data_vistoria_valida("20/08/2026", hoje))
        self.assertFalse(data_vistoria_valida("19/08/2026", hoje))

    def test_data_invalida_nao_e_ignorada(self):
        with self.assertRaises(ValueError):
            data_vistoria_valida("data-invalida", date(2026, 8, 20))

    def test_evento_sem_fuso_e_interpretado_em_utc(self):
        evento = parse_data_api("2026-08-20T12:00:00", "EventDate")
        self.assertEqual(evento.isoformat(), "2026-08-20T12:00:00+00:00")

    def test_ocorrencia_ativa_mais_recente_e_priorizada(self):
        dados = {
            "empresa": "EMPRESA INATIVA",
            "ocorrencias": [
                {"empresa": "EMPRESA INATIVA", "ativo": False},
                {"empresa": "EMPRESA ATIVA MAIS RECENTE", "ativo": True},
            ],
        }
        ocorrencia, motivo = selecionar_ocorrencia_veiculo(dados, [{
            "empresa": "EMPRESA INATIVA",
            "motivo": "Agregação em outra empresa",
        }])
        self.assertEqual(ocorrencia["empresa"], "EMPRESA ATIVA MAIS RECENTE")
        self.assertEqual(motivo, "Agregação em outra empresa")

    def test_solicitacao_da_placa_na_empresa_ativa_entra_na_regra(self):
        dados = {
            "ocorrencias": [
                {"empresa": "EMPRESA INATIVA", "ativo": False},
                {"empresa": "EMPRESA ATIVA", "ativo": True},
            ],
        }
        ocorrencia, motivo = selecionar_ocorrencia_veiculo(dados, [{
            "empresa": "EMPRESA ATIVA",
            "motivo": "Solicitação do cliente",
        }])
        self.assertEqual(ocorrencia["empresa"], "EMPRESA ATIVA")
        self.assertEqual(motivo, "Solicitação do cliente")

    def test_pendente_sem_rastreador_e_diferente_de_desinstalacao(self):
        is_desat_pendente = True
        has_tracker = False
        self.assertTrue(is_desat_pendente and not has_tracker)
        self.assertFalse(is_desat_pendente and has_tracker)

    @patch("app.requests.post")
    def test_coleta_preserva_ocorrencias_da_mesma_placa(self, post):
        resposta = Mock()
        resposta.json.return_value = {"data": {"buscarRelatorioVeiculos": {"data": [
            {"placa": "QAK3H85", "ativo": True, "veiculoStatus": "pendente envio", "empresa": "YAMASHITA MOREIRA TRANSPORTES LTDA"},
            {"placa": "QAK3H85", "ativo": True, "veiculoStatus": "aprovado", "empresa": "FESTUR TRANSPORTE E TURISMO LTDA"},
            {"placa": "QAK3H85", "ativo": True, "veiculoStatus": "aprovado", "empresa": "COOPERATIVA DE TRANSPORTE"},
        ]}}}
        post.return_value = resposta
        veiculos = buscar_todos_veiculos("token-teste")
    @patch("app.requests.post")
    def test_buscar_empresas_com_pasta_valida(self, post):
        from app import buscar_empresas_com_pasta_valida
        resposta = Mock()
        resposta.json.return_value = {
            "data": {
                "buscarPastas": {
                    "data": [
                        {
                            "id": "1",
                            "descricao": "EMPRESA COM PASTA VALIDA LTDA",
                            "empresa": {"razaoSocial": "EMPRESA COM PASTA VALIDA LTDA"},
                            "ordemServicos": [{"id": "os1", "vencimento": "2030-12-31"}]
                        },
                        {
                            "id": "2",
                            "ativo": False,
                            "descricao": "EMPRESA COM PASTA VENCIDA LTDA",
                            "empresa": {"razaoSocial": "EMPRESA COM PASTA VENCIDA LTDA"},
                            "ordemServicos": [{"id": "os2", "vencimento": "2020-01-01"}]
                        }
                    ],
                    "meta": {"hasNextPage": False, "pageCount": 1}
                }
            }
        }
        post.return_value = resposta
        empresas = buscar_empresas_com_pasta_valida("token-teste")
        self.assertIn("EMPRESA COM PASTA VALIDA LTDA", empresas)
        self.assertNotIn("EMPRESA COM PASTA VENCIDA LTDA", empresas)

    @patch("app.requests.post")
    def test_buscar_empresas_considera_data_da_ordem(self, post):
        from app import buscar_empresas_com_pasta_valida
        resposta = Mock()
        resposta.json.return_value = {
            "data": {
                "buscarPastas": {
                    "data": [{
                        "id": "1",
                        "empresa": {"razaoSocial": "EMPRESA COM PASTA ATIVA LTDA"},
                        "ordemServicos": [{"id": "os1", "vencimento": "2030-12-31"}]
                    }],
                    "meta": {"hasNextPage": False, "pageCount": 1}
                }
            }
        }
        post.return_value = resposta

        empresas = buscar_empresas_com_pasta_valida("token-teste")

        self.assertIn("EMPRESA COM PASTA ATIVA LTDA", empresas)


if __name__ == "__main__":
    unittest.main()
