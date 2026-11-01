# ESP-NOW MPU Monitor

Monitor local de telemetria do MPU6050 por ESP-NOW. Inclui firmware emissor e receptor, API Python, dashboard e SQLite.

## Hardware

- 2 ESP32: um conectado ao sensor MPU6050 (emissor) e outro como receptor USB.
- MPU6050 (I2C padrão para ESP32: SDA GPIO21, SCL GPIO22, alimentação compatível com a placa).
- Os dois ESP32 devem estar no mesmo canal Wi-Fi (`CHANNEL=1` no código).
- As bibliotecas **Adafruit MPU6050**, **Adafruit Unified Sensor** e **Adafruit BusIO** são baixadas automaticamente pelo **PlatformIO** via `platformio.ini`.

## Executar (PlatformIO + Python)

1. **Grave o ESP32 Emissor (com MPU6050)** usando o ambiente `sender_mpu6050`:
   ```bash
   pio run -e sender_mpu6050 -t upload
   ```
2. **Grave o ESP32 Receptor (USB)** usando o ambiente `receiver_usb` e mantenha-o conectado via USB ao PC:
   ```bash
   pio run -e receiver_usb -t upload
   ```
   *(Na extensão do PlatformIO no VS Code, basta selecionar o ambiente `env:sender_mpu6050` ou `env:receiver_usb` na barra inferior e clicar em **Upload**).*
3. Com Python 3.10+ instalado, execute na pasta do projeto:

```bash
python -m pip install -r requirements.txt
python server.py --port COM3
```

No Linux, use por exemplo `--port /dev/ttyUSB0`. Feche o monitor serial do PlatformIO antes de iniciar o `server.py`.
Acesse http://127.0.0.1:8000.

## Teste sem hardware (Modo de Simulação Interativo)

```bash
python server.py --demo
```

Você também pode iniciar com `python server.py` normal e ativar/desativar o **Modo Simulação** diretamente pelo botão **🧪 Ativar Modo Simulação** no topo do dashboard (`http://127.0.0.1:8000`).

Recursos do modo de simulação e visualização de chegada:
- **Geração real de quadros `MPU1` (41 bytes)**: passa pelo mesmo decodificador binário, estimador de perda por sequência, janela de pacotes fora de ordem (`missing`) e WebSocket em tempo real.
- **Perfis dinâmicos de movimento do MPU6050**: Inclinação senoidal (`wave`), Rotação 3D contínua (`rotation`), Vibração mecânica de motor (`vibration`), Caminhada/impactos (`steps`) e Repouso em bancada com ruído MEMS (`idle`).
- **Parâmetros de enlace ajustáveis ao vivo**: taxa de amostragem (`1–100 Hz`), perda no rádio (`%`), pacotes fora de ordem (`%`), nível de ruído, múltiplos emissores ESP32 (`1–4 nós`) e opção de gravar ou não no SQLite.
- **Injeção de eventos sob demanda**: botões para disparar **Impacto/Choque (Alta G)**, **Rajada de Perda**, **Pacote Fora de Ordem**, **Drop na Fila USB** e **Reboot do ESP32 (novo `boot_id`)** ou envio passo a passo (**+1 Pacote**).
- **Visualização da chegada de dados**: fita visual de sequência em tempo real (OK, salto/perda, fora de ordem recuperado, duplicado), inspetor do frame binário/hexadecimal, gráficos de **Aceleração (m/s²)**, **Giroscópio (rad/s)**, **Intervalo entre pacotes (`gap_ms`)** e **Atitude 3D estimada (Roll/Pitch)** da placa MPU6050.

## Limitações importantes

- **Perda estimada**: salto de sequência = transmissão não observada pelo receptor. Não distingue falha no rádio, na fila, na serial ou no coletor. Pacotes não capturados no início e fim da sessão não entram na estimativa.
- O **RSSI** é do pacote recebido, não uma medida direta de distância.
- `gap_ms` é o intervalo entre chegadas ao backend, **não** a latência da transmissão ESP-NOW. Latência unidirecional real precisa sincronização de relógios ou mecanismo de RTT.
- Reinicialização do transmissor gera novo `boot_id`; sequências são calculadas separadamente por boot. O reinício do servidor zera os contadores de sessão, embora o SQLite preserve registros.
- O firmware envia pacotes broadcast sem criptografia e presume Arduino-ESP32 3.x. Para transmissão segura, desenvolver protocolo unicast com chaves e autenticação.
- Um receptor ESP-NOW escuta um canal de cada vez. A implementação atual aceita frames do protocolo `MPU1` de 41 bytes.
- Exibição e ingestão em tempo real têm limites práticos: teste perdas de fila serial/USB com a taxa real pretendida.
- Para verificar perda de sensores, também seria preciso detectar falhas de leitura do próprio MPU; a métrica atual mede o fluxo de mensagens sequenciadas.

## Arquivos

- `platformio.ini`: configuração dos dois ambientes PlatformIO (`sender_mpu6050` e `receiver_usb`) e dependências automáticas
- `firmware/sender_mpu6050/main.cpp`: leitura do MPU6050 (I2C) e transmissão ESP-NOW a 50 Hz
- `firmware/receiver_usb/main.cpp`: escuta ESP-NOW, captura de RSSI (compatível com Arduino-ESP32 2.x e 3.x) e encaminhamento JSON via USB
- `server.py`: decodificador binário, perda por sequência, simulador interativo, WebSocket, API e histórico SQLite
- `index.html`: dashboard em tempo real, gráficos, fita de sequência, orientação 3D e painel de simulação
