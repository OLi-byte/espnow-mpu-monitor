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

void onReceive(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  if (!info || !data || len <= 0 || len > MAX_DATA) return;
  Received r = {};
  memcpy(r.mac, info->src_addr, 6);
  r.rssi = info->rx_ctrl ? info->rx_ctrl->rssi : 0;
  r.length = len;
  memcpy(r.data, data, len);
  if (xQueueSend(queuePackets, &r, 0) != pdTRUE) queueDrops++;
}

void setup() {
  Serial.begin(115200);
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(CHANNEL, WIFI_SECOND_CHAN_NONE);
  queuePackets = xQueueCreate(64, sizeof(Received));
  if (!queuePackets || esp_now_init() != ESP_OK) {
    Serial.println("Receiver init failed");
    while (true) delay(1000);
  }
  esp_now_register_recv_cb(onReceive);
}

void loop() {
  Received p;
  if (xQueueReceive(queuePackets, &p, pdMS_TO_TICKS(100)) != pdTRUE) return;
  Serial.printf("{\"mac\":\"%02X:%02X:%02X:%02X:%02X:%02X\",\"rssi\":%d,\"hex\":\"",
    p.mac[0],p.mac[1],p.mac[2],p.mac[3],p.mac[4],p.mac[5],p.rssi);
  for (unsigned i = 0; i < p.length; ++i) Serial.printf("%02X", p.data[i]);
  Serial.printf("\",\"queue_drops\":%lu}\n", (unsigned long)queueDrops);
}
