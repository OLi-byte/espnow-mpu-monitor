#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>

// Arduino-ESP32 3.x; library: Adafruit MPU6050
constexpr int CHANNEL = 1;
constexpr uint32_t SAMPLE_PERIOD_US = 20000; // 50 Hz
Adafruit_MPU6050 mpu;
uint32_t bootId;
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
  Wire.begin();                 // default ESP32 I2C: SDA 21, SCL 22
  if (!mpu.begin()) {
    Serial.println("MPU6050 not found");
    while (true) delay(1000);
  }
  mpu.setAccelerometerRange(MPU6050_RANGE_8_G);
  mpu.setGyroRange(MPU6050_RANGE_500_DEG);
  mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(CHANNEL, WIFI_SECOND_CHAN_NONE);
  bootId = esp_random();
  if (esp_now_init() != ESP_OK) {
    Serial.println("ESP-NOW init failed");
    while (true) delay(1000);
  }
  esp_now_peer_info_t peer = {};
  memset(peer.peer_addr, 0xFF, 6); // broadcast, no encryption
  peer.channel = CHANNEL;
  peer.ifidx = WIFI_IF_STA;
  if (esp_now_add_peer(&peer) != ESP_OK) {
    Serial.println("Cannot register ESP-NOW peer");
    while (true) delay(1000);
  }
}

void loop() {
  static uint32_t nextSample = micros();
  if ((int32_t)(micros() - nextSample) < 0) {
    delay(1);
    return;
  }
  nextSample += SAMPLE_PERIOD_US;
  if ((int32_t)(micros() - nextSample) > 0) nextSample = micros() + SAMPLE_PERIOD_US;

  sensors_event_t a, g, temp;
  mpu.getEvent(&a, &g, &temp);
  SensorFrame frame = {{'M','P','U','1'}, 1, bootId, sequence++, micros(),
                       a.acceleration.x, a.acceleration.y, a.acceleration.z,
                       g.gyro.x, g.gyro.y, g.gyro.z};
  esp_err_t result = esp_now_send((uint8_t*)"\xFF\xFF\xFF\xFF\xFF\xFF",
                                   (uint8_t*)&frame, sizeof(frame));
  if (result != ESP_OK) Serial.printf("Send queue failed: %d\n", result);
}
