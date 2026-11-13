/*
 * SeismoPulse - cuffless blood-pressure TREND monitor
 * Seismocardiogram (aortic-valve opening) -> fingertip PPG pulse transit time
 *
 * Hardware (23MAT204 approved kit only):
 *   Arduino Nano V3        processing, 500 Hz dual-channel acquisition
 *   MPU-6050 (I2C 0x68)    on the sternum: SCG (chest-wall vibration, Z axis)
 *   Pulse sensor (PPG)     on the fingertip: pulse arrival
 *
 * Wiring:
 *   MPU-6050 VCC->5V  GND->GND  SDA->A4  SCL->A5   (module has on-board regulator + pull-ups)
 *   Pulse    +  ->5V  -  ->GND  S  ->A0
 *
 * Serial (115200):  prints  t_ms, HR_bpm, PTT_ms, MAP_est, SYS_est, DIA_est, SQI
 *   Calibration command (from a cuff reading taken at the same time):
 *       C120/80     -> one-point calibration (fixes offset, population slope)
 *       a second C... at a different BP level -> two-point calibration (fits slope)
 *
 * Educational prototype - NOT a medical device. Reports trends, not diagnoses.
 */
#include <Wire.h>
#include <math.h>

// ---------------- configuration ----------------
const uint8_t  MPU_ADDR     = 0x68;
const uint8_t  PPG_PIN      = A0;
const uint8_t  LED_PIN      = 13;
const uint16_t FS           = 500;                 // Hz
const uint32_t TS_US        = 1000000UL / FS;      // 2 ms
const uint16_t BUF_N        = 300;                 // 600 ms SCG history (int16 -> 600 B)

const uint16_t PTT_MIN_MS   = 60,  PTT_MAX_MS = 350;
const uint16_t RR_MIN_MS    = 330, RR_MAX_MS  = 1500;   // 40..180 bpm
const float    PULSE_PRESS  = 40.0f;               // default SBP-DBP until calibrated

// ---------------- state ----------------
int16_t  scgBuf[BUF_N];
uint16_t bufHead = 0;
uint32_t nextUs, sampleIdx = 0;

// SCG band-pass (1st-order HP ~5 Hz + 1st-order LP ~58 Hz, -3 dB points at 500 Hz), fixed-point
int32_t hpPrevIn = 0, hpOut = 0, lpOut = 0;

// PPG processing
int32_t ppgLp = 0, ppgPrevLp = 0, slopeThr = 8, ampThr = 150;
int32_t ppgMin = 0x7FFFFFFF; uint32_t ppgMinIdx = 0;
uint32_t lastFootIdx = 0, refractUntil = 0;
bool     rising = false;
int32_t  peakSlope = 0; uint32_t peakSlopeIdx = 0; int32_t peakSlopeVal = 0;

// beat outputs
float pttEma = 0, hrEma = 0, rrPrev = 0, rmssdAcc = 0; uint16_t rmssdN = 0;
// 32-bit: at 70 bpm a 16-bit count wraps after about 15.6 hours, and since the two counters wrap
// at different times the SQI column then read over 100 % (or divided by zero on the wrap beat).
uint32_t goodBeats = 0, totalBeats = 0;

// calibration: MAP = a + b * ln(PTT_ms)
float calA = 0, calB = -55.0f;                     // population slope (mmHg per ln-unit)
bool  calibrated = false;
float cal1Ln = 0, cal1Map = 0; bool haveCal1 = false;
float pulsePress = PULSE_PRESS;

// ---------------- MPU-6050 ----------------
void mpuWrite(uint8_t reg, uint8_t v) {
  Wire.beginTransmission(MPU_ADDR); Wire.write(reg); Wire.write(v); Wire.endTransmission();
}
int16_t mpuReadAz() {
  Wire.beginTransmission(MPU_ADDR); Wire.write(0x3F); Wire.endTransmission(false);
  Wire.requestFrom(MPU_ADDR, (uint8_t)2);
  // Two statements on purpose: C++ doesn't fix the order in which the operands
  // of `(read() << 8) | read()` are evaluated, so the bytes could swap.
  uint8_t hi = Wire.read();
  uint8_t lo = Wire.read();
  return (int16_t)(((uint16_t)hi << 8) | lo);
}
void mpuInit() {
  mpuWrite(0x6B, 0x00);   // wake up
  mpuWrite(0x1A, 0x02);   // DLPF ~94 Hz (accel), keeps SCG band
  mpuWrite(0x1C, 0x00);   // +-2 g  (16384 LSB/g, ~0.06 mg/LSB)
  mpuWrite(0x19, 0x01);   // sample rate 500 Hz
}

// ---------------- calibration ----------------
void handleSerial() {
  if (!Serial.available()) return;
  if (Serial.peek() != 'C' && Serial.peek() != 'c') { Serial.read(); return; }
  Serial.read();
  int sbp = Serial.parseInt(); int dbp = Serial.parseInt();
  if (sbp < 70 || sbp > 220 || dbp < 40 || dbp >= sbp || pttEma <= 0) {
    Serial.println(F("# calibration rejected (need stable PTT and C<sys>/<dia>)")); return;
  }
  float map_ = dbp + (sbp - dbp) / 3.0f, lnP = log(pttEma);
  pulsePress = sbp - dbp;
  if (haveCal1 && fabs(lnP - cal1Ln) > 0.03f) {    // two-point: fit slope
    calB = (map_ - cal1Map) / (lnP - cal1Ln);
    calB = constrain(calB, -150.0f, -10.0f);
  } else if (haveCal1) {
    // PTT changed by under ~3 %, too little to fit a slope from: say so, rather than letting
    // a second reading look like a successful two-point calibration.
    Serial.println(F("# PTT barely changed since the last reading: kept the old slope (one-point)."));
    Serial.println(F("# For two-point, raise the BP more (exercise) before the second cuff reading."));
  }
  calA = map_ - calB * lnP;
  cal1Ln = lnP; cal1Map = map_; haveCal1 = true; calibrated = true;
  Serial.print(F("# calibrated: MAP = ")); Serial.print(calA, 1);
  Serial.print(F(" + ")); Serial.print(calB, 1); Serial.println(F(" * ln(PTT)"));
}

// ---------------- beat processing ----------------
void onBeat(uint32_t footIdx) {
  totalBeats++;
  uint32_t rr = (footIdx - lastFootIdx) * (1000 / FS);
  lastFootIdx = footIdx;

  // search SCG history for aortic-opening (AO) peak in [foot-350 ms, foot-60 ms]
  // (SCG band-pass and PPG low-pass delays are constant -> absorbed by calibration)
  uint32_t lagSamples = sampleIdx - footIdx;                 // foot is a few samples in the past
  int16_t best = -32767; int16_t bestK = -1;
  for (uint16_t k = PTT_MIN_MS / 2; k < PTT_MAX_MS / 2 && k + lagSamples < BUF_N; k++) {
    int16_t v = scgBuf[(bufHead + BUF_N - 1 - lagSamples - k) % BUF_N];
    if (v > best) { best = v; bestK = k; }
  }
  if (bestK < 0) { rrPrev = 0; return; }
  float ptt = bestK * (1000.0f / FS);

  // signal-quality index: physiological ranges + beat-to-beat consistency
  bool ok = rr >= RR_MIN_MS && rr <= RR_MAX_MS && ptt > PTT_MIN_MS && ptt < PTT_MAX_MS;
  if (ok && pttEma > 0 && fabs(ptt - pttEma) > 0.25f * pttEma) ok = false;
  if (!ok) {
    // Seed the average only from a plausible beat (as sim/seismopulse_sim.py does): a PTT stuck on
    // the edge of the search window means the SCG had no clear peak, and would anchor the EMA there.
    if (pttEma == 0 && rr >= RR_MIN_MS && rr <= RR_MAX_MS && ptt > PTT_MIN_MS && ptt < PTT_MAX_MS) pttEma = ptt;
    rrPrev = 0;   // RMSSD needs back-to-back intervals: don't difference across a rejected beat
    return;
  }

  goodBeats++;
  pttEma = pttEma == 0 ? ptt : 0.85f * pttEma + 0.15f * ptt;
  float hr = 60000.0f / rr;
  hrEma = hrEma == 0 ? hr : 0.8f * hrEma + 0.2f * hr;
  if (rrPrev > 0) { float d = rr - rrPrev; rmssdAcc += d * d; rmssdN++; }
  rrPrev = rr;

  float mapE = calA + calB * log(pttEma);
  Serial.print(millis()); Serial.print(',');
  Serial.print(hrEma, 1); Serial.print(',');
  Serial.print(pttEma, 1); Serial.print(',');
  if (calibrated) {
    Serial.print(mapE, 1); Serial.print(',');
    Serial.print(mapE + 2 * pulsePress / 3, 0); Serial.print(',');
    Serial.print(mapE - pulsePress / 3, 0);
  } else Serial.print(F("NA,NA,NA"));
  Serial.print(','); Serial.println((100UL * goodBeats) / totalBeats);
  if (rmssdN >= 30) {
    Serial.print(F("# RMSSD_ms=")); Serial.println(sqrt(rmssdAcc / rmssdN), 1);
    rmssdAcc = 0; rmssdN = 0;
  }
  digitalWrite(LED_PIN, HIGH);
}

void setup() {
  Serial.begin(115200);
  // parseInt() waits this long for digits that haven't arrived. At the default 1 s, a stray
  // "C" (or a line cut short) stopped sampling for 2 s, and loop() then raced through the
  // missed samples back to back, squeezing 2 s of signal into a burst. A whole "C120/80" line
  // takes under 1 ms at 115200 baud, so 20 ms is plenty.
  Serial.setTimeout(20);
  pinMode(LED_PIN, OUTPUT);
  Wire.begin(); Wire.setClock(400000);
  mpuInit();
  Serial.println(F("# SeismoPulse ready: MPU on sternum, pulse sensor on fingertip, sit still"));
  Serial.println(F("# t_ms,HR_bpm,PTT_ms,MAP,SYS,DIA,SQI%"));
  ppgLp = ppgPrevLp = (int32_t)analogRead(PPG_PIN) << 4;   // avoid start-up transient
  hpPrevIn = mpuReadAz();
  nextUs = micros() + TS_US;
}

void loop() {
  handleSerial();
  if ((int32_t)(micros() - nextUs) < 0) return;
  nextUs += TS_US;
  sampleIdx++;

  // ---- SCG: band-pass ~5..58 Hz on accel Z ----
  int32_t az = mpuReadAz();
  hpOut = ((hpOut * 15) >> 4) + az - hpPrevIn;     // HP, a = 0.9375 (~5 Hz @500 Hz)
  hpPrevIn = az;
  lpOut += ((hpOut - lpOut) * 2) >> 2;             // LP, a = 0.5 (-3 dB at ~58 Hz @500 Hz)
  scgBuf[bufHead] = (int16_t)constrain(lpOut, -32767, 32767);
  bufHead = (bufHead + 1) % BUF_N;

  // ---- PPG: smoothing + max-slope foot detection ----
  int32_t raw = analogRead(PPG_PIN) << 4;
  ppgLp += (raw - ppgLp) >> 3;                     // LP ~10 Hz
  int32_t slope = ppgLp - ppgPrevLp; ppgPrevLp = ppgLp;

  if (sampleIdx > refractUntil && sampleIdx - lastFootIdx > 2UL * FS) { slopeThr = 8; ampThr = 150; }  // lost lock
  if (!rising && ppgLp < ppgMin) { ppgMin = ppgLp; ppgMinIdx = sampleIdx; }
  if (sampleIdx > refractUntil && slope > slopeThr) {
    rising = true;
    if (slope > peakSlope) { peakSlope = slope; peakSlopeIdx = sampleIdx; peakSlopeVal = ppgLp; }
  } else if (rising && slope <= 0) {
    // intersecting-tangent foot: where the max-slope line crosses the preceding minimum
    int32_t amp = ppgLp - ppgMin;
    if (amp > ampThr) {                                      // reject noise wiggles
      int32_t dt = peakSlope > 0 ? (peakSlopeVal - ppgMin) / peakSlope : 0;
      uint32_t foot = peakSlopeIdx - (uint32_t)constrain(dt, 0, 50);
      // adaptive thresholds ~40 % of recent beats, capped against artefact spikes
      slopeThr = max(4L, (slopeThr * 3 + (min(peakSlope, 3 * slopeThr) * 2) / 5) >> 2);
      ampThr   = max(60L, (ampThr * 3 + (min(amp, 3 * ampThr) * 2) / 5) >> 2);
      onBeat(foot);
      refractUntil = sampleIdx + 125;                        // 250 ms
    }
    rising = false; peakSlope = 0; ppgMin = 0x7FFFFFFF;
  }
  if (sampleIdx - lastFootIdx > 30) digitalWrite(LED_PIN, LOW);
}
