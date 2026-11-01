#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

constexpr int CHANNEL = 1;
constexpr int MAX_DATA = 250;

struct Received {
  uint8_t mac[6];
  int8_t rssi;
  uint16_t length;
  uint8_t data[MAX_DATA];
};

QueueHandle_t queuePackets;
volatile uint32_t queueDrops = 0;

#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
void onReceive(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  if (!info || !data || len <= 0 || len > MAX_DATA) return;
  Received r = {};
  memcpy(r.mac, info->src_addr, 6);
  r.rssi = info->rx_ctrl ? info->rx_ctrl->rssi : 0;
  r.length = len;
  memcpy(r.data, data, len);
  if (xQueueSend(queuePackets, &r, 0) != pdTRUE) queueDrops = queueDrops + 1;
}
#else
volatile int8_t lastRssi = 0;

void promiscuousRxCb(void *buf, wifi_promiscuous_pkt_type_t type) {
  if (type != WIFI_PKT_MGMT && type != WIFI_PKT_DATA) return;
  const auto *pkt = static_cast<const wifi_promiscuous_pkt_t *>(buf);
  lastRssi = pkt->rx_ctrl.rssi;
}

void onReceive(const uint8_t *mac, const uint8_t *data, int len) {
  if (!mac || !data || len <= 0 || len > MAX_DATA) return;
  Received r = {};
  memcpy(r.mac, mac, 6);
  r.rssi = lastRssi;
  r.length = len;
  memcpy(r.data, data, len);
  if (xQueueSend(queuePackets, &r, 0) != pdTRUE) queueDrops = queueDrops + 1;
}
#endif

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
  esp_wifi_set_ps(WIFI_PS_NONE); // Desativa power-save do Wi-Fi para latência mínima
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(CHANNEL, WIFI_SECOND_CHAN_NONE);
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  esp_wifi_set_promiscuous(false);
#else
  esp_wifi_set_promiscuous_rx_cb(&promiscuousRxCb);
#endif
  queuePackets = xQueueCreate(64, sizeof(Received));
  if (!queuePackets || esp_now_init() != ESP_OK) {
    while (true) {
      Serial.println("{\"type\":\"diag\",\"role\":\"receiver\",\"status\":\"espnow_error\",\"msg\":\"Falha ao iniciar receptor ESP-NOW\"}");
      delay(1000);
    }
  }
  esp_now_register_recv_cb(onReceive);
  Serial.printf("{\"type\":\"diag\",\"role\":\"receiver\",\"status\":\"listening\",\"mac\":\"%s\",\"channel\":%d}\n",
                WiFi.macAddress().c_str(), CHANNEL);
}

void loop() {
  static constexpr char HEX_LUT[] = "0123456789ABCDEF";
  static uint32_t lastDiagMs = 0;
  Received p;
  if (xQueueReceive(queuePackets, &p, pdMS_TO_TICKS(50)) != pdTRUE) {
    if (millis() - lastDiagMs >= 1500) {
      lastDiagMs = millis();
      Serial.printf("{\"type\":\"diag\",\"role\":\"receiver\",\"status\":\"listening\",\"mac\":\"%s\",\"channel\":%d}\n",
                    WiFi.macAddress().c_str(), CHANNEL);
    }
    return;
  }
  char line[620];
  int pos = snprintf(line, sizeof(line),
                     "{\"mac\":\"%02X:%02X:%02X:%02X:%02X:%02X\",\"rssi\":%d,\"hex\":\"",
                     p.mac[0], p.mac[1], p.mac[2], p.mac[3], p.mac[4], p.mac[5],
                     p.rssi);
  for (unsigned i = 0; i < p.length && (pos + 2) < (int)sizeof(line) - 32; ++i) {
    line[pos++] = HEX_LUT[(p.data[i] >> 4) & 0x0F];
    line[pos++] = HEX_LUT[p.data[i] & 0x0F];
  }
  pos += snprintf(line + pos, sizeof(line) - pos,
                  "\",\"queue_drops\":%lu}\n", (unsigned long)queueDrops);
  Serial.write(reinterpret_cast<const uint8_t *>(line), pos);
}
