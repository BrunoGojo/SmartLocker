#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kiosk SmartLocker com:
- Preview camera (OpenCV -> Tkinter)
- Cadastro protegido por login de administrador (SQLite + bcrypt)
- Teclado virtual automático (onboard)
- Reconhecimento / Treinar via API
- Controle de solenoide (opcional)
"""

import cv2
import threading
import time
import io
import requests
from PIL import Image, ImageTk
import tkinter as tk
from tkinter import messagebox
import sqlite3
import bcrypt
import subprocess
import shutil
import os
import sys
import traceback

# ---------- CONFIGURAÇÕES ----------
# IMPORTANTE: altere API_URL para o IP/host correto da sua API enquanto não usar domínio
API_URL = "https://smartlocktests-a5c9bxa6gqewehcj.brazilsouth-01.azurewebsites.net"  # ex: "http://192.168.1.50:8000"
ADMIN_TOKEN = "b77d74d1a7f4f83fcb134b4d8a09fdcd0a4b4921b739e84de3d6a29e43e1cfb3"

USE_GPIO = True  # True se for usar o pino GPIO para solenoide
SOLENOID_PIN = 17
CAPTURE_IMAGES_PER_USER = 5  # fotos por usuário no cadastro
CAMERA_INDEX = 0  # índice da câmera
DATABASE_FILE = "smartlocker.db"

# se True, chama /train automaticamente após envio bem-sucedido do cadastro
AUTO_TRAIN_AFTER_UPLOAD = False
# ------------------------------------

# --------- Verifica disponibilidade do 'onboard' (teclado virtual) ----------
ONBOARD_CMD = shutil.which("onboard")


def show_keyboard():
    if ONBOARD_CMD:
        try:
            subprocess.Popen([ONBOARD_CMD])
        except Exception as e:
            print("Falha ao abrir onboard:", e)
    else:
        # apenas logar; não é crítico
        print("onboard não encontrado. Instale com: sudo apt install onboard")


def hide_keyboard():
    if ONBOARD_CMD:
        try:
            # tenta matar usando o caminho exato
            subprocess.Popen(["pkill", "-f", ONBOARD_CMD])
        except Exception:
            # fallback: pkill onboard
            try:
                subprocess.Popen(["pkill", "onboard"])
            except Exception:
                pass


# ---------------- GPIO (opcional) ----------------
if USE_GPIO:
    try:
        import RPi.GPIO as GPIO

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(SOLENOID_PIN, GPIO.OUT)
        GPIO.output(SOLENOID_PIN, GPIO.LOW)
        GPIO_AVAILABLE = True
    except Exception as e:
        print("GPIO indisponível (ou não estamos em uma RPi):", e)
        GPIO_AVAILABLE = False
else:
    GPIO_AVAILABLE = False

# ----------------- Banco de Dados (SQLite) -----------------


def init_db(db_file=DATABASE_FILE):
    """Cria banco e tabela de admins, e cria um admin padrão se não existir."""
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash BLOB NOT NULL
    )
    """)
    conn.commit()

    # verifica se já existe algum admin
    cur.execute("SELECT COUNT(*) FROM admins")
    row = cur.fetchone()
    if row and row[0] == 0:
        # criar admin padrão: admin / admin123 (mude depois)
        default_user = "admin"
        default_pw = "admin123".encode("utf-8")
        hashed = bcrypt.hashpw(default_pw, bcrypt.gensalt())
        try:
            cur.execute("INSERT INTO admins (username, password_hash) VALUES (?, ?)",
                        (default_user, hashed))
            conn.commit()
            print("Admin padrão criado: usuario='admin' senha='admin123' -> ALTERE ESSA SENHA IMEDIATAMENTE")
        except Exception as e:
            print("Falha ao criar admin padrão:", e)
    conn.close()


def check_admin_login(username, password, db_file=DATABASE_FILE):
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    cur.execute("SELECT password_hash FROM admins WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return False
    stored = row[0]
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored)
    except Exception as e:
        print("Erro na verificação do bcrypt:", e)
        return False


def change_admin_password(username, new_password, db_file=DATABASE_FILE):
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    hashed = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt())
    cur.execute("UPDATE admins SET password_hash = ? WHERE username = ?", (hashed, username))
    conn.commit()
    conn.close()


# inicializa DB na primeira execução
init_db()

# ------------------- App Tkinter -------------------
class KioskApp:
    def __init__(self, root, fullscreen=True):
        self.root = root
        self.fullscreen = fullscreen

        # estado de autenticação admin
        self.admin_authenticated = False
        self.admin_user = None

        # inicializa câmera
        self.cap = cv2.VideoCapture(CAMERA_INDEX)
        # tentativas para garantir câmera
        if not self.cap.isOpened():
            print("Atenção: câmera não abriu no índice", CAMERA_INDEX)

        # tenta configurar resolução básica
        try:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        except Exception:
            pass

        self.captured_images = []  # lista de bytes das imagens capturadas (cadastro)
        self.setup_ui()
        self.running = True
        self.update_frame()

    def setup_ui(self):
        self.root.title("SmartLocker Kiosk")
        if self.fullscreen:
            self.root.attributes("-fullscreen", True)
            self.root.configure(bg="black")
        else:
            self.root.geometry("1000x640")

        preview_frame = tk.Frame(self.root, bg="black")
        preview_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8, pady=8)

        controls_frame = tk.Frame(self.root, bg="#222", width=380)
        controls_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=8, pady=8)

        self.canvas = tk.Label(preview_frame, bg="black")
        self.canvas.pack(fill=tk.BOTH, expand=True)

        font_title = ("Helvetica", 18, "bold")
        font_btn = ("Helvetica", 16)
        font_small = ("Helvetica", 12)

        # Cadastro
        tk.Label(controls_frame, text="Cadastro de Usuário", bg="#222", fg="white", font=font_title).pack(pady=(6,4))
        self.name_entry = tk.Entry(controls_frame, font=font_btn, justify="center")
        self.name_entry.pack(pady=(0,8), ipadx=6, ipady=8)
        # dispara teclado virtual quando campo recebe foco
        self.name_entry.bind("<FocusIn>", lambda e: show_keyboard())
        self.name_entry.bind("<FocusOut>", lambda e: hide_keyboard())

        btn_frame = tk.Frame(controls_frame, bg="#222")
        btn_frame.pack(pady=(4,12))

        self.capture_btn = tk.Button(btn_frame, text="Capturar Foto", font=font_btn, width=18, height=2,
                                     command=self.capture_image, bg="#007ACC", fg="white")
        self.capture_btn.grid(row=0, column=0, padx=6, pady=6)

        self.send_btn = tk.Button(btn_frame, text="Enviar Cadastro", font=font_btn, width=18, height=2,
                                  command=self.send_registration, bg="#16A085", fg="white")
        self.send_btn.grid(row=1, column=0, padx=6, pady=6)

        self.captures_label = tk.Label(controls_frame, text=f"Fotos capturadas: 0 / {CAPTURE_IMAGES_PER_USER}", bg="#222",
                                       fg="white", font=font_small)
        self.captures_label.pack(pady=(0,8))

        # Admin login / status
        admin_frame = tk.Frame(controls_frame, bg="#222")
        admin_frame.pack(pady=(8,8))
        self.admin_status = tk.Label(admin_frame, text="Admin: Não autenticado", bg="#222", fg="red", font=font_small)
        self.admin_status.grid(row=0, column=0, padx=6)
        self.admin_btn = tk.Button(admin_frame, text="Login Admin", command=self.admin_login_popup)
        self.admin_btn.grid(row=0, column=1, padx=6)

        # Reconhecimento
        tk.Label(controls_frame, text="Reconhecimento", bg="#222", fg="white", font=font_title).pack(pady=(14,4))
        self.recognize_btn = tk.Button(controls_frame, text="Reconhecer Agora", font=font_btn, width=22, height=2,
                                       command=self.recognize_once, bg="#E67E22", fg="white")
        self.recognize_btn.pack(pady=(6,6))

        self.recognize_result = tk.Label(controls_frame, text="Resultado: —", bg="#222", fg="white", font=font_small)
        self.recognize_result.pack(pady=(4,8))

        self.train_btn = tk.Button(controls_frame, text="Treinar Modelos (API)", font=font_btn, width=22, height=2,
                                   command=self.train_models, bg="#2980B9", fg="white")
        self.train_btn.pack(pady=(6,6))

        self.open_btn = tk.Button(controls_frame, text="Abrir Locker (Manual)", font=font_btn, width=22, height=2,
                                  command=self.open_locker_manual, bg="#2ECC71", fg="white")
        self.open_btn.pack(pady=(6,6))

        bottom_frame = tk.Frame(controls_frame, bg="#222")
        bottom_frame.pack(side=tk.BOTTOM, pady=12)

        self.mode_btn = tk.Button(bottom_frame, text="Alternar Modo Tela", command=self.toggle_fullscreen)
        self.mode_btn.grid(row=0, column=0, padx=6)

        self.exit_btn = tk.Button(bottom_frame, text="Sair", command=self.quit_app)
        self.exit_btn.grid(row=0, column=1, padx=6)

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        self.root.attributes("-fullscreen", self.fullscreen)

    def update_frame(self):
        if not self.running:
            return

        try:
            ret, frame = self.cap.read()
        except Exception:
            ret = False
            frame = None

        if ret and frame is not None:
            # Converte BGR para RGB
            try:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            except Exception:
                pass

            # Obtém dimensões e redimensiona mantendo aspecto corretamente
            try:
                height, width = frame.shape[:2]
                canvas_width = max(self.canvas.winfo_width(), 1)  # evita 0
                canvas_height = max(self.canvas.winfo_height(), 1)  # evita 0
                ratio = min(canvas_width / width, canvas_height / height)
                new_width = max(int(width * ratio), 1)
                new_height = max(int(height * ratio), 1)
                frame = cv2.resize(frame, (new_width, new_height))
            except Exception:
                # se algo falhar no resize, ignoramos e mostramos tamanho original
                pass

            # Converte para formato Tkinter
            try:
                img = Image.fromarray(frame)
                imgtk = ImageTk.PhotoImage(image=img)
                # Mantém referência e atualiza imagem
                self.canvas.imgtk = imgtk
                self.canvas.configure(image=imgtk)
            except Exception as e:
                # Não travar o loop caso falhe conversão imagem->tk
                print("Erro ao atualizar preview:", e)

        # Agenda próxima atualização
        self.root.after(30, self.update_frame)

    def capture_image(self):
        if not self.admin_authenticated:
            messagebox.showwarning("Acesso negado", "Somente administradores podem capturar para cadastro. Faça login.")
            return
        try:
            ret, frame = self.cap.read()
        except Exception as e:
            ret = False
            frame = None
            print("Erro ao ler câmera:", e)

        if not ret or frame is None:
            messagebox.showerror("Erro", "Não foi possível acessar a câmera.")
            return
        _, buf = cv2.imencode('.jpg', frame)
        img_bytes = buf.tobytes()
        # evita capturar mais do que o limite (só loga e informa)
        if len(self.captured_images) >= CAPTURE_IMAGES_PER_USER:
            messagebox.showinfo("Info", f"Você já capturou {CAPTURE_IMAGES_PER_USER} fotos. Pressione 'Enviar Cadastro' ou remova fotos manualmente.")
            return
        self.captured_images.append(img_bytes)
        self.captures_label.config(text=f"Fotos capturadas: {len(self.captured_images)} / {CAPTURE_IMAGES_PER_USER}")
        if len(self.captured_images) >= CAPTURE_IMAGES_PER_USER:
            messagebox.showinfo("Info", f"{CAPTURE_IMAGES_PER_USER} fotos capturadas. Pressione 'Enviar Cadastro'.")

    def send_registration(self):
        if not self.admin_authenticated:
            messagebox.showwarning("Acesso negado", "Somente administradores podem enviar cadastros. Faça login.")
            return
        user_name = self.name_entry.get().strip()
        if user_name == "":
            messagebox.showwarning("Aviso", "Digite o nome do usuário.")
            return
        if len(self.captured_images) == 0:
            messagebox.showwarning("Aviso", "Nenhuma foto capturada.")
            return

        def worker():
            try:
                headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
                success = True
                for i, img_bytes in enumerate(self.captured_images, start=1):
                    url = f"{API_URL}/add-user/{user_name}"
                    # requests aceita bytes em files, mas precisamos colocar um file-like ou tupla (nome, bytes, content-type)
                    files = {"file": (f"img{i}.jpg", img_bytes, "image/jpeg")}
                    try:
                        resp = requests.post(url, files=files, headers=headers, timeout=20)
                    except Exception as e:
                        success = False
                        print(f"[ADD-USER] Erro ao enviar foto {i} para {url}: {e}")
                        messagebox.showerror("Erro", f"Falha ao enviar foto {i}: {e}")
                        break

                    print(f"[ADD-USER] foto {i} status: {resp.status_code} | resp: {resp.text}")

                    if resp.status_code in (200, 201):
                        # ok para essa imagem, continuar
                        continue
                    elif resp.status_code == 401:
                        success = False
                        messagebox.showerror("Não autorizado", "Token inválido ou ausente ao enviar cadastro.")
                        break
                    else:
                        success = False
                        # tenta obter JSON para mensagem mais clara
                        try:
                            msg = resp.json()
                        except Exception:
                            msg = resp.text
                        messagebox.showerror("Erro", f"Falha ao enviar foto {i}: {resp.status_code} - {msg}")
                        break

                if success:
                    # opcional: auto-treinar após upload
                    if AUTO_TRAIN_AFTER_UPLOAD:
                        try:
                            t_resp = requests.post(f"{API_URL}/train", headers=headers, timeout=120)
                            print(f"[AUTO-TRAIN] status: {t_resp.status_code} | {t_resp.text}")
                            if t_resp.status_code in (200,):
                                messagebox.showinfo("Sucesso", f"Envio concluído para '{user_name}'. Treinamento iniciado.")
                            elif t_resp.status_code == 401:
                                messagebox.showwarning("Treino", "Envio OK, mas treino não autorizado (token).")
                            else:
                                messagebox.showwarning("Treino", f"Envio OK, resposta treino inesperada: {t_resp.status_code}")
                        except Exception as e:
                            print("[AUTO-TRAIN] erro:", e)
                            messagebox.showwarning("Treino", f"Envio OK, mas falha ao iniciar treino: {e}")
                    else:
                        messagebox.showinfo("Sucesso", f"Envio concluído para '{user_name}'.")
                    self.captured_images.clear()
                    self.captures_label.config(text=f"Fotos capturadas: 0 / {CAPTURE_IMAGES_PER_USER}")
            except Exception as e:
                traceback.print_exc()
                messagebox.showerror("Erro", f"Falha no envio: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def recognize_once(self):
        ret, frame = self.cap.read()
        if not ret:
            messagebox.showerror("Erro", "Falha ao capturar imagem.")
            return
        _, buf = cv2.imencode('.jpg', frame)
        img_bytes = buf.tobytes()

        def worker():
            try:
                url = f"{API_URL}/recognize"
                files = {"file": ("image.jpg", img_bytes, "image/jpeg")}
                try:
                    resp = requests.post(url, files=files, timeout=15)
                except Exception as e:
                    print("[RECOGNIZE] Erro na requisição:", e)
                    self.recognize_result.config(text="Resultado: Erro de conexão")
                    messagebox.showerror("Erro", f"Erro no reconhecimento (conexão): {e}")
                    return

                status = resp.status_code
                text = resp.text
                print(f"[RECOGNIZE] status: {status} | resp: {text}")

                # tenta parsear JSON com segurança
                try:
                    data = resp.json()
                except Exception:
                    data = None

                if status != 200:
                    # mostrar mensagem apropriada
                    if status == 401:
                        self.recognize_result.config(text="Resultado: Não autorizado")
                        messagebox.showerror("Erro", "Reconhecimento não autorizado (token/API).")
                    else:
                        self.recognize_result.config(text="Resultado: Erro servidor")
                        messagebox.showerror("Erro", f"Resposta inesperada do servidor: {status}\n{text}")
                    return

                if not data:
                    self.recognize_result.config(text="Resultado: Resposta inválida")
                    messagebox.showerror("Erro", "Resposta do servidor inválida.")
                    return

                if data.get("found"):
                    user = data.get("user", "Desconhecido")
                    conf = data.get("confidence", 0)
                    self.recognize_result.config(text=f"Resultado: {user} ({conf:.1f})")
                    # abrir locker (thread-safe)
                    try:
                        self.open_locker()
                    except Exception as e:
                        print("Erro ao abrir locker:", e)
                else:
                    # diferencia motivos se disponíveis
                    reason = data.get("reason", "")
                    if reason:
                        self.recognize_result.config(text=f"Não reconhecido: {reason}")
                    else:
                        self.recognize_result.config(text="Resultado: Não reconhecido")
            except Exception as e:
                traceback.print_exc()
                self.recognize_result.config(text="Resultado: Erro")
                messagebox.showerror("Erro", f"Erro no reconhecimento: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def train_models(self):
        def worker():
            try:
                url = f"{API_URL}/train"
                headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
                try:
                    resp = requests.post(url, headers=headers, timeout=120)
                except Exception as e:
                    print("[TRAIN] Erro na requisição:", e)
                    messagebox.showerror("Erro", f"Falha ao chamar /train: {e}")
                    return
                print(f"[TRAIN] status: {resp.status_code} | resp: {resp.text}")
                if resp.status_code == 200:
                    try:
                        j = resp.json()
                        # mensagem mais amigável se houver 'results'
                        messagebox.showinfo("Treino", "Treinamento concluído (verifique logs da API).")
                    except Exception:
                        messagebox.showinfo("Treino", "Treinamento concluído.")
                elif resp.status_code == 401:
                    messagebox.showerror("Não autorizado", "Token inválido ou ausente na API!")
                else:
                    messagebox.showwarning("Treino", f"Resposta inesperada: {resp.status_code}\n{resp.text}")
            except Exception as e:
                traceback.print_exc()
                messagebox.showerror("Erro", f"Falha ao chamar /train: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def open_locker_manual(self):
        if not self.admin_authenticated:
            if not messagebox.askyesno("Confirmar", "Abrir manualmente requer autenticação admin. Deseja realizar login agora?"):
                return
            self.admin_login_popup()
            return
        if messagebox.askyesno("Confirmar", "Deseja abrir o locker manualmente?"):
            self.open_locker()

    def open_locker(self):
        if GPIO_AVAILABLE:
            try:
                GPIO.output(SOLENOID_PIN, GPIO.HIGH)
                time.sleep(2)
                GPIO.output(SOLENOID_PIN, GPIO.LOW)
            except Exception as e:
                messagebox.showerror("Erro GPIO", f"Falha ao acionar GPIO: {e}")
        else:
            # modo simulado para desenvolvimento
            messagebox.showinfo("Simulação", "Locker aberto (simulado).")

    def quit_app(self):
        if messagebox.askyesno("Sair", "Deseja realmente sair?"):
            self.running = False
            try:
                self.cap.release()
            except:
                pass
            if GPIO_AVAILABLE:
                try:
                    GPIO.cleanup()
                except:
                    pass
            hide_keyboard()
            self.root.destroy()

    # ---------------- Admin login popup ----------------
    def admin_login_popup(self):
        login_win = tk.Toplevel(self.root)
        login_win.title("Login do Administrador")
        login_win.geometry("420x360")
        login_win.configure(bg="#222")
        login_win.transient(self.root)
        login_win.grab_set()

        tk.Label(login_win, text="Login Admin", font=("Helvetica", 20, "bold"), bg="#222", fg="white").pack(pady=10)

        tk.Label(login_win, text="Usuário:", bg="#222", fg="white").pack(pady=(6,0))
        username_entry = tk.Entry(login_win, font=("Helvetica", 14))
        username_entry.pack(ipadx=8, ipady=6, pady=(0,8))
        username_entry.bind("<FocusIn>", lambda e: show_keyboard())
        username_entry.bind("<FocusOut>", lambda e: hide_keyboard())

        tk.Label(login_win, text="Senha:", bg="#222", fg="white").pack(pady=(4,0))
        password_entry = tk.Entry(login_win, font=("Helvetica", 14), show="*")
        password_entry.pack(ipadx=8, ipady=6, pady=(0,8))
        password_entry.bind("<FocusIn>", lambda e: show_keyboard())
        password_entry.bind("<FocusOut>", lambda e: hide_keyboard())

        def try_login():
            user = username_entry.get().strip()
            pw = password_entry.get().strip()
            if user == "" or pw == "":
                messagebox.showwarning("Aviso", "Preencha usuário e senha.")
                return
            ok = check_admin_login(user, pw)
            if ok:
                hide_keyboard()
                self.admin_authenticated = True
                self.admin_user = user
                self.admin_status.config(text=f"Admin: {user}", fg="lightgreen")
                login_win.destroy()
                messagebox.showinfo("Bem-vindo", f"Autenticado como {user}")
            else:
                messagebox.showerror("Erro", "Usuário ou senha inválidos.")

        btn_frame = tk.Frame(login_win, bg="#222")
        btn_frame.pack(pady=10)

        tk.Button(btn_frame, text="Entrar", font=("Helvetica", 14), bg="#007ACC", fg="white",
                  width=12, height=2, command=try_login).grid(row=0, column=0, padx=6)
        tk.Button(btn_frame, text="Cancelar", font=("Helvetica", 14), bg="#888", fg="white",
                  width=12, height=2, command=lambda: (hide_keyboard(), login_win.destroy())).grid(row=0, column=1, padx=6)

        # Opção para alterar senha (apenas se autenticado, aqui mostramos para admin atual)
        def change_pw_popup():
            if not self.admin_authenticated:
                messagebox.showwarning("Aviso", "Autentique-se primeiro para alterar senha.")
                return
            cp = tk.Toplevel(self.root)
            cp.title("Alterar Senha Admin")
            cp.geometry("420x240")
            cp.transient(self.root)
            cp.grab_set()
            tk.Label(cp, text="Nova senha:", bg="#222", fg="white").pack(pady=(12,4))
            new_pw_entry = tk.Entry(cp, font=("Helvetica", 14), show="*")
            new_pw_entry.pack(ipadx=8, ipady=6, pady=(0,8))
            new_pw_entry.bind("<FocusIn>", lambda e: show_keyboard())
            new_pw_entry.bind("<FocusOut>", lambda e: hide_keyboard())

            def do_change():
                new_pw = new_pw_entry.get().strip()
                if new_pw == "":
                    messagebox.showwarning("Aviso", "Senha inválida.")
                    return
                change_admin_password(self.admin_user, new_pw)
                hide_keyboard()
                cp.destroy()
                messagebox.showinfo("Sucesso", "Senha alterada.")

            tk.Button(cp, text="Alterar", font=("Helvetica", 12), command=do_change).pack(pady=8)

        # botão para alterar senha será habilitado só após login; deixamos ele visível, mas somente funcional após autenticação
        tk.Button(login_win, text="Alterar senha (após login)", command=change_pw_popup).pack(pady=(6,0))


# ----------------- Execução -----------------
def main():
    root = tk.Tk()
    app = KioskApp(root, fullscreen=True)
    root.mainloop()


if __name__ == "__main__":
    main()