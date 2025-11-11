import os
import io
import numpy as np
import cv2
import tempfile
from typing import Dict, List, Tuple
from pathlib import Path
from .storage import get_storage
import threading

# --- Configurações ---
IMG_SIZE = (100, 100)
# Threshold maior para LBPH (quanto maior, mais permissivo)
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "130.0"))

storage = get_storage()
lock = threading.Lock()

# --- Cache em memória ---
_cached_recognizers: List[Tuple[object, int]] = []
_cached_label_dict: Dict[int, str] = {}
_cache_loaded: bool = False
_cache_lock = threading.Lock()


def _next_image_filename(existing_count: int) -> str:
    return f"img{existing_count + 1}.jpg"


def save_user_image(user: str, file_bytes: bytes, filename: str = None) -> str:
    imgs = storage.list_user_images(user)
    count = len(imgs)
    if filename is None:
        filename = _next_image_filename(count)
    path = storage.save_image(user, filename, file_bytes)

    # Invalida cache para forçar reload
    global _cache_loaded
    with _cache_lock:
        _cache_loaded = False
    return path


def list_users() -> List[str]:
    return storage.list_users()


def delete_user(user: str) -> bool:
    ok = storage.delete_user(user)
    global _cache_loaded
    with _cache_lock:
        _cache_loaded = False
    return ok


def train_all() -> Dict[str, str]:
    """
    Treina um modelo LBPH para cada usuário com suas imagens
    e salva no storage. Cada usuário recebe um label único.
    """
    results = {}
    with lock:
        users = list_users()
        if not users:
            return {}

        label_id = 0
        for user in users:
            images = storage.list_user_images(user)
            if not images:
                results[user] = "no-images"
                continue

            faces = []
            labels = []

            for img_ref in images:
                local_path = storage.download_to_temp(img_ref)
                img = cv2.imread(local_path, cv2.IMREAD_GRAYSCALE)
                try:
                    os.remove(local_path)
                except Exception:
                    pass

                if img is None:
                    continue

                img_resized = cv2.resize(img, IMG_SIZE)
                faces.append(img_resized)
                labels.append(label_id)

            if not faces:
                results[user] = "no-valid-images"
                continue

            recognizer = cv2.face.LBPHFaceRecognizer_create()
            recognizer.train(faces, np.array(labels))

            tmpfile = tempfile.NamedTemporaryFile(delete=False, suffix=".yml")
            tmpfile.close()
            recognizer.save(tmpfile.name)

            with open(tmpfile.name, "rb") as f:
                model_bytes = f.read()

            os.unlink(tmpfile.name)

            import random, string
            model_id = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
            model_filename = f"{user}_trainer_{model_id}.yml"
            path = storage.save_model(user, model_filename, model_bytes)
            results[user] = path

            label_id += 1  # Próximo usuário ganha outro label

    # Recarrega os modelos em memória
    load_models_into_cache(force=True)
    return results


def load_models_into_cache(force: bool = False) -> None:
    """Carrega todos os modelos salvos no storage para a memória."""
    global _cached_recognizers, _cached_label_dict, _cache_loaded

    with _cache_lock:
        if _cache_loaded and not force:
            return

        models = storage.list_models()
        recognizers: List[Tuple[object, int]] = []
        label_dict: Dict[int, str] = {}
        label_id = 0

        for user, model_ref in models:
            try:
                local_model = storage.download_to_temp(model_ref)
                recognizer = cv2.face.LBPHFaceRecognizer_create()
                recognizer.read(local_model)
                recognizers.append((recognizer, label_id))
                label_dict[label_id] = user
                label_id += 1
                try:
                    os.remove(local_model)
                except Exception:
                    pass
            except Exception:
                continue

        _cached_recognizers = recognizers
        _cached_label_dict = label_dict
        _cache_loaded = True


def force_reload_cache() -> None:
    """Força o recarregamento dos modelos."""
    load_models_into_cache(force=True)


def recognize_image_bytes(img_bytes: bytes) -> Dict:
    """Recebe bytes de imagem e tenta reconhecer o rosto."""
    load_models_into_cache()

    arr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return {"found": False, "error": "invalid-image"}

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    face_cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    faces = face_cascade.detectMultiScale(gray, 1.2, 5)

    if len(faces) == 0:
        return {"found": False, "reason": "no-face-detected"}

    recognizers = _cached_recognizers
    label_dict = _cached_label_dict
    if not recognizers:
        return {"found": False, "reason": "no-models"}

    best = {"label": None, "confidence": float("inf")}
    for (x, y, w, h) in faces:
        roi_gray = gray[y:y + h, x:x + w]
        roi_resized = cv2.resize(roi_gray, IMG_SIZE)
        for recognizer, label in recognizers:
            try:
                pred_label, confidence = recognizer.predict(roi_resized)
            except Exception:
                continue
            if confidence < best["confidence"]:
                best = {"label": label, "confidence": float(confidence)}

    if best["label"] is None:
        return {"found": False, "reason": "no-prediction"}

    # LBPH: menor valor de confiança = melhor correspondência
    if best["confidence"] < CONFIDENCE_THRESHOLD:
        user = label_dict.get(best["label"], "unknown")
        return {"found": True, "user": user, "confidence": float(best["confidence"])}
    else:
        return {
            "found": False,
            "confidence": float(best["confidence"]),
            "reason": "low-confidence",
        }


def list_models() -> List[Tuple[str, str]]:
    """Lista os modelos disponíveis no storage."""
    return storage.list_models()

