# Front-locker-patch/Front-locker.py
# Exemplo: trecho que envia imagens para o endpoint remoto com token no header
import requests

API_URL = "https://smartlocktests-a5c9bxa6gqewehcj.brazilsouth-01.azurewebsites.net"  # ajuste
ADMIN_TOKEN = "b77d74d1a7f4f83fcb134b4d8a09fdcd0a4b4921b739e84de3d6a29e43e1cfb3"

# enviar uma imagem para criar usuário
def upload_image(user_name: str, image_path: str):
    url = f"{API_URL}/add-user/{user_name}"
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    with open(image_path, "rb") as f:
        files = {"file": (image_path, f, "image/jpeg")}
        resp = requests.post(url, files=files, headers=headers, timeout=20)
    print(resp.status_code, resp.text)

# chamar reconhecimento (não requer token no nosso design)
def recognize(image_path: str):
    url = f"{API_URL}/recognize"
    with open(image_path, "rb") as f:
        files = {"file": (image_path, f, "image/jpeg")}
        resp = requests.post(url, files=files, timeout=20)
    print(resp.status_code, resp.json())
