import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("AGEMS_SECRET_KEY", "test-secret")
os.environ.setdefault("SYSTEMSAT_HASH_AUTH", "test-hash")
os.environ.setdefault("SYSTEMSAT_USERNAME", "test-user")
os.environ.setdefault("SYSTEMSAT_PASSWORD", "test-password")

from app import (
    calcular_haversine_metros,
    analisar_posicoes_veiculo,
    ConectividadeAudit,
    Veiculo,
    VeiculoAudit,
    app,
    db
)

class ConectividadeTest(unittest.TestCase):
    def test_calculo_haversine_distancia(self):
        # Coordenadas aproximadas em Campo Grande (~1.1 km)
        lat1, lon1 = -20.4428, -54.6167
        lat2, lon2 = -20.4500, -54.6250
        dist = calcular_haversine_metros(lat1, lon1, lat2, lon2)
        self.assertGreater(dist, 1000)
        self.assertLess(dist, 1300)

    def test_haversine_mesmo_ponto(self):
        dist = calcular_haversine_metros(-20.4428, -54.6167, -20.4428, -54.6167)
        self.assertEqual(round(dist, 2), 0.0)

    def test_regra_ignicao_violada_com_mais_de_3_posicoes(self):
        # 4 posições com velocidade > 20 e ignição False
        posicoes = [
            {"Latitude": -20.44, "Longitude": -54.61, "Velocity": 35.0, "Ignition": False, "EventDate": "2026-09-01T10:00:00"},
            {"Latitude": -20.4401, "Longitude": -54.6101, "Velocity": 40.0, "Ignition": False, "EventDate": "2026-09-01T10:01:00"},
            {"Latitude": -20.4402, "Longitude": -54.6102, "Velocity": 25.0, "Ignition": False, "EventDate": "2026-09-01T10:02:00"},
            {"Latitude": -20.4403, "Longitude": -54.6103, "Velocity": 30.0, "Ignition": False, "EventDate": "2026-09-01T10:03:00"},
            {"Latitude": -20.4404, "Longitude": -54.6104, "Velocity": 10.0, "Ignition": True, "EventDate": "2026-09-01T10:04:00"},
        ]
        resultado = analisar_posicoes_veiculo(posicoes)
        self.assertEqual(resultado["status_analise"], "ERRO")
        self.assertIn("Sensor de ignição violado", resultado["anomalias"])
        self.assertEqual(resultado["qtd_violacoes_ignicao"], 4)

    def test_regra_ignicao_normal_ate_3_posicoes_nao_marca_erro(self):
        # Apenas 2 posições com velocidade > 20 e ignição False (<= 3 não gera erro)
        posicoes = [
            {"Latitude": -20.44, "Longitude": -54.61, "Velocity": 35.0, "Ignition": False, "EventDate": "2026-09-01T10:00:00"},
            {"Latitude": -20.4401, "Longitude": -54.6101, "Velocity": 40.0, "Ignition": False, "EventDate": "2026-09-01T10:01:00"},
            {"Latitude": -20.4402, "Longitude": -54.6102, "Velocity": 15.0, "Ignition": False, "EventDate": "2026-09-01T10:02:00"},
            {"Latitude": -20.4403, "Longitude": -54.6103, "Velocity": 50.0, "Ignition": True, "EventDate": "2026-09-01T10:03:00"},
            {"Latitude": -20.4404, "Longitude": -54.6104, "Velocity": 10.0, "Ignition": True, "EventDate": "2026-09-01T10:04:00"},
        ]
        resultado = analisar_posicoes_veiculo(posicoes)
        self.assertEqual(resultado["status_analise"], "OK")
        self.assertNotIn("Sensor de ignição violado", resultado["anomalias"])
        self.assertEqual(resultado["qtd_violacoes_ignicao"], 2)

    def test_regra_saltos_durante_percurso_maior_20_porcento(self):
        # 6 posições, saltos maiores que 1000m
        posicoes = [
            {"Latitude": -20.4400, "Longitude": -54.6100, "Velocity": 30.0, "Ignition": True, "EventDate": "2026-09-01T10:00:00"},
            {"Latitude": -20.4550, "Longitude": -54.6250, "Velocity": 30.0, "Ignition": True, "EventDate": "2026-09-01T10:01:00"}, # ~2.2km (>1000m)
            {"Latitude": -20.4700, "Longitude": -54.6400, "Velocity": 30.0, "Ignition": True, "EventDate": "2026-09-01T10:02:00"}, # ~2.2km (>1000m)
            {"Latitude": -20.4701, "Longitude": -54.6401, "Velocity": 30.0, "Ignition": True, "EventDate": "2026-09-01T10:03:00"}, # ~15m
            {"Latitude": -20.4702, "Longitude": -54.6402, "Velocity": 30.0, "Ignition": True, "EventDate": "2026-09-01T10:04:00"}, # ~15m
            {"Latitude": -20.4703, "Longitude": -54.6403, "Velocity": 30.0, "Ignition": True, "EventDate": "2026-09-01T10:05:00"}, # ~15m
        ]
        # 2 saltos em 5 transições = 40% (> 20%)
        resultado = analisar_posicoes_veiculo(posicoes)
        self.assertEqual(resultado["status_analise"], "ERRO")
        self.assertIn("SALTOS DURANTE O PERCURSO", resultado["anomalias"])
        self.assertGreater(resultado["perc_saltos"], 20.0)

    def test_posicoes_vazias_retorna_alerta(self):
        resultado = analisar_posicoes_veiculo([])
        self.assertEqual(resultado["status_analise"], "ALERTA")
        self.assertEqual(resultado["total_posicoes"], 0)

if __name__ == "__main__":
    unittest.main()
