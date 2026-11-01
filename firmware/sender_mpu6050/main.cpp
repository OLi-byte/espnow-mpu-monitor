#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>

constexpr int CHANNEL = 1;
constexpr uint32_t SAMPLE_PERIOD_US = 13333; // 75 Hz (~13.3 ms) ideal para 115200 bps sem fila
constexpr uint8_t BROADCAST_MAC[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

Adafruit_MPU6050 mpu;
uint32_t bootId = 0;
uint32_t sequence = 0;

struct __attribute__((packed)) SensorFrame {
  char magic[4];                 // "MPU1"
  uint8_t version;
  uint32_t boot_id;
  uint32_t sequence;
  uint32_t uptime_us;
  float ax, ay, az;              // m/s^2
  float gx, gy, gz;              // rad/s
};
static_assert(sizeof(SensorFrame) == 41, "Unexpected frame layout");

void setup() {
  Serial.begin(115200);
  delay(50);

  WiFi.mode(WIFI_STA);
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  while (!WiFi.STA.started()) {
    delay(10);
  }
#endif
  WiFi.disconnect();
  esp_wifi_set_ps(WIFI_PS_NONE); // Sem economia de energia no rádio (menor latência)
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(CHANNEL, WIFI_SECOND_CHAN_NONE);
  esp_wifi_set_promiscuous(false);

  Wire.begin(21, 22);            // I2C padrão ESP32: SDA = GPIO 21, SCL = GPIO 22
  Wire.setClock(400000);         // I2C Fast Mode 400 kHz (~0.45 ms por leitura)
  while (!mpu.begin()) {
    Serial.printf("{\"type\":\"diag\",\"role\":\"sender\",\"status\":\"mpu_error\",\"mac\":\"%s\",\"msg\":\"MPU6050 nao encontrado (SDA=21, SCL=22)\"}\n",
                  WiFi.macAddress().c_str());
    delay(1000);
  }
  mpu.setAccelerometerRange(MPU6050_RANGE_8_G);
  mpu.setGyroRange(MPU6050_RANGE_500_DEG);
  mpu.setFilterBandwidth(MPU6050_BAND_94_HZ); // Menor atraso de fase interno (~2.8 ms)

  bootId = esp_random();
  if (esp_now_init() != ESP_OK) {
    while (true) {
      Serial.println("{\"type\":\"diag\",\"role\":\"sender\",\"status\":\"espnow_error\",\"msg\":\"Falha ao iniciar ESP-NOW\"}");
      delay(1000);
    }
  }

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, BROADCAST_MAC, 6); // Broadcast sem criptografia
  peer.channel = CHANNEL;
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;
  if (esp_now_add_peer(&peer) != ESP_OK) {
    while (true) {
      Serial.println("{\"type\":\"diag\",\"role\":\"sender\",\"status\":\"peer_error\",\"msg\":\"Falha ao registrar peer broadcast\"}");
      delay(1000);
    }
  }
  Serial.printf("{\"type\":\"diag\",\"role\":\"sender\",\"status\":\"ok\",\"mac\":\"%s\",\"channel\":%d}\n",
                WiFi.macAddress().c_str(), CHANNEL);
}

void loop() {
  static uint32_t nextSample = micros();
  static uint32_t lastDiagMs = 0;
  int32_t remainingUs = (int32_t)(nextSample - micros());
  if (remainingUs > 0) {
    if (remainingUs > 1500) {
      delay(1);
    } else {
      delayMicroseconds(remainingUs);
    }
    return;
  }
  nextSample += SAMPLE_PERIOD_US;
  if ((int32_t)(micros() - nextSample) > (int32_t)SAMPLE_PERIOD_US) {
    nextSample = micros() + SAMPLE_PERIOD_US;
  }

  sensors_event_t a, g, temp;
  mpu.getEvent(&a, &g, &temp);
  SensorFrame frame = {
      {'M', 'P', 'U', '1'},
      1,
      bootId,
      sequence++,
      micros(),
      a.acceleration.x,
      a.acceleration.y,
      a.acceleration.z,
      g.gyro.x,
      g.gyro.y,
      g.gyro.z};

  esp_err_t result = esp_now_send(
      BROADCAST_MAC, reinterpret_cast<const uint8_t *>(&frame), sizeof(frame));
  if (millis() - lastDiagMs >= 1500) {
    lastDiagMs = millis();
    Serial.printf("{\"type\":\"diag\",\"role\":\"sender\",\"status\":\"%s\",\"mac\":\"%s\",\"seq\":%lu}\n",
                  result == ESP_OK ? "ok" : "tx_error",
                  WiFi.macAddress().c_str(),
                  (unsigned long)sequence);
  }
}
