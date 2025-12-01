# finger_service.py
import time
import serial
import adafruit_fingerprint

class FingerprintService:
    def __init__(self, port="/dev/serial0", baudrate=57600):
        self.sensor = None
        self.available = False
        try:
            # Configura a conexão serial UART
            uart = serial.Serial(port, baudrate=baudrate, timeout=1)
            self.sensor = adafruit_fingerprint.Adafruit_Fingerprint(uart)
            self.available = self.sensor.check_module()
            if self.available:
                print(f"[Biometria] Sensor encontrado! Templates salvos: {self.sensor.count}")
            else:
                print("[Biometria] Sensor não respondeu.")
        except Exception as e:
            print(f"[Biometria] Erro ao inicializar: {e}")
            self.available = False

    def find_empty_slot(self):
        """Busca o próximo ID livre (0-127)"""
        if not self.available: return None
        # O sensor R307 geralmente suporta ids de 1 a 127 (ou mais dependendo do modelo)
        # Vamos tentar achar um buraco livre
        # A biblioteca não tem um 'get_free_id' nativo eficiente, então iteramos ou confiamos no count
        # Para simplificar, vamos tentar do 1 ao 127
        for i in range(1, 128):
            if self.sensor.load_model(i) != adafruit_fingerprint.OK:
                # Se falhar ao carregar, provavelmente está vazio
                return i
        return None

    def enroll_finger(self, location_id):
        """
        Realiza o processo de cadastro completo.
        Retorna True se sucesso, False caso contrário.
        Este método é 'blocante', ideal rodar em thread.
        """
        if not self.available: return False

        print(f"[Biometria] Coloque o dedo para cadastrar na posição {location_id}...")
        
        # 1. Primeira captura
        while self.sensor.get_image() != adafruit_fingerprint.OK:
            pass # Esperando dedo
        
        print("[Biometria] Imagem 1 capturada.")
        if self.sensor.image_2_tz(1) != adafruit_fingerprint.OK:
            return False

        print("[Biometria] Remova o dedo...")
        time.sleep(1)
        while self.sensor.get_image() != adafruit_fingerprint.NOFINGER:
            pass

        print("[Biometria] Coloque o MESMO dedo novamente...")
        
        # 2. Segunda captura (confirmação)
        while self.sensor.get_image() != adafruit_fingerprint.OK:
            pass

        print("[Biometria] Imagem 2 capturada.")
        if self.sensor.image_2_tz(2) != adafruit_fingerprint.OK:
            return False

        # 3. Cria o modelo
        if self.sensor.create_model() != adafruit_fingerprint.OK:
            print("[Biometria] As digitais não coincidem.")
            return False
        
        # 4. Salva no slot
        if self.sensor.store_model(location_id) != adafruit_fingerprint.OK:
            print("[Biometria] Erro ao salvar na memória flash.")
            return False

        print(f"[Biometria] Sucesso! Salvo no ID {location_id}")
        return True

    def check_finger(self):
        """
        Verifica se há um dedo e se ele é reconhecido.
        Retorna o ID (int) se achou, ou None.
        """
        if not self.available: return None

        # Tenta ler a imagem
        if self.sensor.get_image() != adafruit_fingerprint.OK:
            return None
        
        # Converte para template
        if self.sensor.image_2_tz(1) != adafruit_fingerprint.OK:
            return None
        
        # Busca no banco de dados interno do sensor
        if self.sensor.finger_search() != adafruit_fingerprint.OK:
            return None
        
        return self.sensor.finger_id

    def delete_finger(self, location_id):
        if not self.available: return False
        return self.sensor.delete_model(location_id) == adafruit_fingerprint.OK