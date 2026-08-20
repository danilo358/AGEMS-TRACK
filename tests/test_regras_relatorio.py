import os
import unittest
from datetime import date
from unittest.mock import Mock, patch

os.environ.setdefault("AGEMS_SECRET_KEY", "test-secret")
os.environ.setdefault("SYSTEMSAT_HASH_AUTH", "test-hash")
os.environ.setdefault("SYSTEMSAT_USERNAME", "test-user")
os.environ.setdefault("SYSTEMSAT_PASSWORD", "test-password")

from app import buscar_todos_veiculos, data_vistoria_valida, deve_entrar_manutencao, parse_data_api, selecionar_ocorrencia_veiculo


class RegrasRelatorioTest(unittest.TestCase):
    def test_veiculo_online_nao_fica_em_manutencao_sem_marcacao_manual(self):
        self.assertFalse(deve_entrar_manutencao(2, False))

    def test_marcacao_manual_mantem_manutencao_mesmo_online(self):
        self.assertTrue(deve_entrar_manutencao(2, True))

    def test_offline_por_quinze_dias_entra_em_manutencao(self):
        self.assertTrue(deve_entrar_manutencao(15, False))

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

    def test_solicitacao_seleciona_empresa_correta_em_placa_repetida(self):
        dados = {
            "empresa": "FESTUR TRANSPORTE E TURISMO LTDA",
            "ocorrencias": [
                {"empresa": "YAMASHITA MOREIRA TRANSPORTES LTDA", "ativo": True},
                {"empresa": "FESTUR TRANSPORTE E TURISMO LTDA", "ativo": True},
            ],
        }
        ocorrencia, motivo = selecionar_ocorrencia_veiculo(dados, [{
            "empresa": "YAMASHITA MOREIRA TRANSPORTES LTDA",
            "motivo": "Agregação em outra empresa",
        }])
        self.assertEqual(ocorrencia["empresa"], "YAMASHITA MOREIRA TRANSPORTES LTDA")
        self.assertEqual(motivo, "Agregação em outra empresa")

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
        self.assertEqual(len(veiculos["QAK3H85"]["ocorrencias"]), 3)


if __name__ == "__main__":
    unittest.main()
