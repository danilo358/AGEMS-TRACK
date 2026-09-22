import os
import unittest
from datetime import datetime, timezone, timedelta

os.environ.setdefault("AGEMS_SECRET_KEY", "test-secret")
os.environ.setdefault("SYSTEMSAT_HASH_AUTH", "test-hash")
os.environ.setdefault("SYSTEMSAT_USERNAME", "test-user")
os.environ.setdefault("SYSTEMSAT_PASSWORD", "test-password")

from app import detectar_viagens_geofence, viagem_pertence_data

class AnaliseGradeTest(unittest.TestCase):
    def test_detectar_viagens_geofence_simples(self):
        ponto_inicial = [-54.6201, -20.4697]
        ponto_final = [-54.8064, -22.2211]

        t0 = datetime(2026, 9, 22, 8, 0, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(minutes=10)
        t2 = t0 + timedelta(hours=1)
        t3 = t0 + timedelta(hours=2, minutes=30)

        posicoes = [
            {"datetime": t0, "timestamp_str": t0.strftime("%d/%m/%Y %H:%M:%S"), "hora_str": "08:00:00", "lat": -20.4800, "lng": -54.6201, "vel": 0},
            {"datetime": t1, "timestamp_str": t1.strftime("%d/%m/%Y %H:%M:%S"), "hora_str": "08:10:00", "lat": -20.4697, "lng": -54.6201, "vel": 10},
            {"datetime": t2, "timestamp_str": t2.strftime("%d/%m/%Y %H:%M:%S"), "hora_str": "09:00:00", "lat": -21.3000, "lng": -54.7000, "vel": 80},
            {"datetime": t3, "timestamp_str": t3.strftime("%d/%m/%Y %H:%M:%S"), "hora_str": "10:30:00", "lat": -22.2211, "lng": -54.8064, "vel": 5},
        ]

        viagens = detectar_viagens_geofence(
            posicoes=posicoes,
            ponto_inicial_coord=ponto_inicial,
            ponto_final_coord=ponto_final,
            raio_tolerancia_m=100
        )

        self.assertEqual(len(viagens), 1)
        v = viagens[0]
        self.assertEqual(v["inicio_hora"], "09:00:00")
        self.assertEqual(v["fim_hora"], "10:30:00")
        self.assertEqual(v["duracao_minutos"], 90.0)
        self.assertIn("01h 30m 00s", v["duracao_formatada"])
        self.assertEqual(v["amostras_gps"], 2)

    def test_detectar_viagens_multiplos_ciclos(self):
        ponto_inicial = [-54.6201, -20.4697]
        ponto_final = [-54.8064, -22.2211]

        t0 = datetime(2026, 9, 22, 6, 0, 0, tzinfo=timezone.utc)

        posicoes = [
            # Viagem 1
            {"datetime": t0, "timestamp_str": t0.strftime("%d/%m/%Y %H:%M:%S"), "hora_str": "06:00:00", "lat": -20.4697, "lng": -54.6201, "vel": 10},
            {"datetime": t0 + timedelta(hours=1), "timestamp_str": (t0 + timedelta(hours=1)).strftime("%d/%m/%Y %H:%M:%S"), "hora_str": "07:00:00", "lat": -21.3000, "lng": -54.7000, "vel": 70},
            {"datetime": t0 + timedelta(hours=2), "timestamp_str": (t0 + timedelta(hours=2)).strftime("%d/%m/%Y %H:%M:%S"), "hora_str": "08:00:00", "lat": -22.2211, "lng": -54.8064, "vel": 0},

            # Viagem 2
            {"datetime": t0 + timedelta(hours=8), "timestamp_str": (t0 + timedelta(hours=8)).strftime("%d/%m/%Y %H:%M:%S"), "hora_str": "14:00:00", "lat": -20.4697, "lng": -54.6201, "vel": 15},
            {"datetime": t0 + timedelta(hours=9), "timestamp_str": (t0 + timedelta(hours=9)).strftime("%d/%m/%Y %H:%M:%S"), "hora_str": "15:00:00", "lat": -21.3000, "lng": -54.7000, "vel": 65},
            {"datetime": t0 + timedelta(hours=10, minutes=30), "timestamp_str": (t0 + timedelta(hours=10, minutes=30)).strftime("%d/%m/%Y %H:%M:%S"), "hora_str": "16:30:00", "lat": -22.2211, "lng": -54.8064, "vel": 0},
        ]

        viagens = detectar_viagens_geofence(
            posicoes=posicoes,
            ponto_inicial_coord=ponto_inicial,
            ponto_final_coord=ponto_final,
            raio_tolerancia_m=100
        )

        self.assertEqual(len(viagens), 2)
        self.assertEqual(viagens[0]["duracao_minutos"], 60.0)
        self.assertEqual(viagens[1]["duracao_minutos"], 90.0)

    def test_ignora_microdeslocamento_parado_antes_da_saida_real(self):
        ponto_inicial = [-54.6201, -20.4697]
        ponto_final = [-54.8064, -22.2211]
        t0 = datetime(2026, 9, 22, 4, 0, 0, tzinfo=timezone.utc)

        def posicao(momento, lat, lng, vel=0):
            return {
                "datetime": momento,
                "timestamp_str": momento.strftime("%d/%m/%Y %H:%M:%S"),
                "hora_str": momento.strftime("%H:%M:%S"),
                "lat": lat,
                "lng": lng,
                "vel": vel,
            }

        viagens = detectar_viagens_geofence(
            [
                posicao(t0, -20.4697, -54.6201),
                posicao(t0 + timedelta(minutes=1), -20.4690, -54.6201, 8),
                posicao(t0 + timedelta(minutes=10), -20.4690, -54.6201),
                posicao(t0 + timedelta(hours=1), -20.4700, -54.6201, 20),
                posicao(t0 + timedelta(hours=1, minutes=10), -20.5000, -54.6300, 60),
                posicao(t0 + timedelta(hours=2), -22.2211, -54.8064),
            ],
            ponto_inicial,
            ponto_final,
            raio_tolerancia_m=100,
        )

        self.assertEqual(len(viagens), 1)
        self.assertEqual(viagens[0]["inicio_hora"], "05:10:00")

    def test_cancela_saida_de_patio_antes_da_viagem_real(self):
        ponto_inicial = [-54.6201, -20.4697]
        ponto_final = [-54.8064, -22.2211]
        t0 = datetime(2026, 9, 22, 11, 0, 0, tzinfo=timezone.utc)

        def posicao(momento, lat, lng, vel=0):
            return {
                "datetime": momento,
                "timestamp_str": momento.strftime("%d/%m/%Y %H:%M:%S"),
                "hora_str": momento.strftime("%H:%M:%S"),
                "lat": lat,
                "lng": lng,
                "vel": vel,
            }

        viagens = detectar_viagens_geofence(
            [
                posicao(t0, -20.4697, -54.6201),
                posicao(t0 + timedelta(minutes=1), -20.4740, -54.6201, 20),
                posicao(t0 + timedelta(minutes=5), -20.4697, -54.6201),
                posicao(t0 + timedelta(hours=4), -20.4697, -54.6201),
                posicao(t0 + timedelta(hours=4, minutes=1), -20.4740, -54.6201, 20),
                posicao(t0 + timedelta(hours=4, minutes=10), -21.3000, -54.7000, 70),
                posicao(t0 + timedelta(hours=5), -22.2211, -54.8064),
            ],
            ponto_inicial,
            ponto_final,
            raio_tolerancia_m=100,
        )

        self.assertEqual(len(viagens), 1)
        self.assertEqual(viagens[0]["inicio_hora"], "15:01:00")

    def test_detectar_viagens_incompleta(self):
        ponto_inicial = [-54.6201, -20.4697]
        ponto_final = [-54.8064, -22.2211]

        t0 = datetime(2026, 9, 22, 6, 0, 0, tzinfo=timezone.utc)

        posicoes = [
            {"datetime": t0, "timestamp_str": t0.strftime("%d/%m/%Y %H:%M:%S"), "hora_str": "06:00:00", "lat": -20.4697, "lng": -54.6201, "vel": 10},
            {"datetime": t0 + timedelta(hours=1), "timestamp_str": (t0 + timedelta(hours=1)).strftime("%d/%m/%Y %H:%M:%S"), "hora_str": "07:00:00", "lat": -21.3000, "lng": -54.7000, "vel": 70},
        ]

        viagens = detectar_viagens_geofence(
            posicoes=posicoes,
            ponto_inicial_coord=ponto_inicial,
            ponto_final_coord=ponto_final,
            raio_tolerancia_m=100
        )

        self.assertEqual(len(viagens), 0)

    def test_detectar_viagem_registra_horarios_dos_pontos_intermediarios(self):
        ponto_inicial = [-54.6201, -20.4697]
        ponto_intermediario = [-54.7000, -21.3000]
        ponto_final = [-54.8064, -22.2211]
        t0 = datetime(2026, 9, 22, 8, 0, 0, tzinfo=timezone.utc)

        def posicao(momento, lat, lng):
            return {
                "datetime": momento,
                "timestamp_str": momento.strftime("%d/%m/%Y %H:%M:%S"),
                "hora_str": momento.strftime("%H:%M:%S"),
                "lat": lat,
                "lng": lng,
                "vel": 50,
            }

        viagens = detectar_viagens_geofence(
            [
                posicao(t0, -20.4697, -54.6201),
                posicao(t0 + timedelta(minutes=45), -21.3000, -54.7000),
                posicao(t0 + timedelta(hours=2), -22.2211, -54.8064),
            ],
            ponto_inicial,
            ponto_final,
            pontos_rota=[
                {"nome": "Origem", "coord": ponto_inicial},
                {"nome": "Parada central", "coord": ponto_intermediario},
                {"nome": "Destino", "coord": ponto_final},
            ],
        )

        self.assertEqual(len(viagens), 1)
        self.assertEqual(
            viagens[0]["horarios_pontos"][1],
            {
                "nome": "Parada central",
                "hora": "08:45:00",
                "timestamp": "22/09/2026 08:45:00",
            },
        )

    def test_detectar_viagem_nao_exige_todos_os_pontos_intermediarios(self):
        ponto_inicial = [-54.6201, -20.4697]
        ponto_intermediario = [-54.7000, -21.3000]
        ponto_final = [-54.8064, -22.2211]
        t0 = datetime(2026, 9, 22, 8, 0, 0, tzinfo=timezone.utc)

        def posicao(momento, lat, lng):
            return {
                "datetime": momento,
                "timestamp_str": momento.strftime("%d/%m/%Y %H:%M:%S"),
                "hora_str": momento.strftime("%H:%M:%S"),
                "lat": lat,
                "lng": lng,
                "vel": 50,
            }

        viagens = detectar_viagens_geofence(
            [
                posicao(t0, -20.4697, -54.6201),
                posicao(t0 + timedelta(hours=1), -21.5000, -54.9000),
                posicao(t0 + timedelta(hours=2), -22.2211, -54.8064),
            ],
            ponto_inicial,
            ponto_final,
            pontos_rota=[
                {"nome": "Origem", "coord": ponto_inicial},
                {"nome": "Parada não registrada", "coord": ponto_intermediario},
                {"nome": "Destino", "coord": ponto_final},
            ],
        )

        self.assertEqual(len(viagens), 1)
        self.assertEqual(len(viagens[0]["horarios_pontos"]), 2)
        self.assertEqual(viagens[0]["inicio_hora"], "09:00:00")

    def test_registra_ponto_posterior_mesmo_se_anterior_nao_for_detectado(self):
        ponto_inicial = [-54.6201, -20.4697]
        ponto_nao_detectado = [-54.7000, -21.3000]
        ponto_sidrolandia = [-54.9000, -21.7000]
        ponto_final = [-54.8064, -22.2211]
        t0 = datetime(2026, 9, 22, 8, 0, 0, tzinfo=timezone.utc)

        def posicao(momento, lat, lng):
            return {
                "datetime": momento,
                "timestamp_str": momento.strftime("%d/%m/%Y %H:%M:%S"),
                "hora_str": momento.strftime("%H:%M:%S"),
                "lat": lat,
                "lng": lng,
                "vel": 50,
            }

        viagens = detectar_viagens_geofence(
            [
                posicao(t0, -20.4697, -54.6201),
                posicao(t0 + timedelta(minutes=45), -21.7000, -54.9000),
                posicao(t0 + timedelta(hours=2), -22.2211, -54.8064),
            ],
            ponto_inicial,
            ponto_final,
            raio_tolerancia_m=100,
            pontos_rota=[
                {"nome": "Origem", "coord": ponto_inicial},
                {"nome": "Ponto não detectado", "coord": ponto_nao_detectado},
                {"nome": "Sidrolândia", "coord": ponto_sidrolandia},
                {"nome": "Destino", "coord": ponto_final},
            ],
        )

        self.assertEqual(len(viagens), 1)
        self.assertEqual(viagens[0]["horarios_pontos"][1]["nome"], "Sidrolândia")
        self.assertEqual(viagens[0]["horarios_pontos"][1]["hora"], "08:45:00")

    def test_inclui_viagem_noturna_que_termina_na_data_consultada(self):
        viagem = {
            "inicio_datetime": "2026-09-20T21:28:57-04:00",
            "fim_datetime": "2026-09-21T10:55:24-04:00",
        }

        self.assertTrue(viagem_pertence_data(viagem, "2026-09-21"))
        self.assertFalse(viagem_pertence_data(viagem, "2026-09-22"))

if __name__ == "__main__":
    unittest.main()
