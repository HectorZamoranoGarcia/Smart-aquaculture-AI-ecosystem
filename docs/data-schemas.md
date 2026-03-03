# Data Schemas — OceanTrust AI

> **Maintainer:** Cloud Architecture Team
> **Version:** 1.0.0
> **Last Updated:** 2026-03-03

This document defines the canonical message contracts for all data produced into the Redpanda/Kafka message bus. Every producer **MUST** validate its output against the corresponding JSON Schema before publishing. Consumers **MUST NOT** assume backward-compatible changes without a schema registry version bump.

---

## Table of Contents

1. [Design Principles](#1-design-principles)
2. [Schema Registry Strategy](#2-schema-registry-strategy)
3. [IoT Sensor Event — Salmon Farm Telemetry](#3-iot-sensor-event--salmon-farm-telemetry)
4. [Oslo Fish Market Data Event](#4-oslo-fish-market-data-event)
5. [Envelope Metadata Fields (Common)](#5-envelope-metadata-fields-common)
6. [Versioning & Evolution Rules](#6-versioning--evolution-rules)

---

## 1. Design Principles

| Principle | Decision |
|-----------|----------|
| **Serialization format** | JSON (v1.x), migration to Avro/Protobuf in v2 |
| **Timestamp standard** | RFC 3339 / ISO 8601 with UTC offset (`+00:00`) |
| **Units** | SI system throughout (Celsius, mg/L, NOK/kg) |
| **Nullability** | Explicit `null` for missing sensor readings — never omit the field |
| **Enum casing** | `SCREAMING_SNAKE_CASE` for all enum values |
| **IDs** | UUIDv4 for all entity and event identifiers |
| **Decimal precision** | Financial values: 4 decimal places. Sensor values: 3 decimal places |

---

## 2. Schema Registry Strategy

All schemas live in `/schemas/` and are registered in **Confluent Schema Registry** (bundled with the Redpanda deployment in `/bin`). Topics follow the subject naming convention:

```
{topic-name}-value
```

Example:
- `salmon.farm.sensor.telemetry-value`  → `iot_sensor_event.schema.json`
- `market.oslo.fish.prices-value`       → `market_data_event.schema.json`

---

## 3. IoT Sensor Event — Salmon Farm Telemetry

**Kafka Topic:** `salmon.farm.sensor.telemetry`
**Schema File:** `/schemas/iot_sensor_event.schema.json`
**Partition Key:** `farm_id`
**Expected Frequency:** Every **30 seconds** per sensor unit

### 3.1 Full Example Payload

```json
{
  "schema_version": "1.0.0",
  "event_id": "a3f1c2d4-8b7e-4e1a-9f2b-3c5d6e7f8a9b",
  "event_type": "SENSOR_TELEMETRY",
  "timestamp": "2026-03-03T18:30:00+00:00",
  "producer": {
    "device_id": "SENSOR-NO-FARM07-CAGE03-UNIT02",
    "device_type": "MULTIPARAMETER_PROBE",
    "firmware_version": "3.2.1",
    "manufacturer": "AquaSense"
  },
  "location": {
    "farm_id": "NO-FARM-0047",
    "farm_name": "Hardangerfjord Alpha Station",
    "cage_id": "CAGE-03",
    "coordinates": {
      "latitude": 60.1533,
      "longitude": 6.0142,
      "depth_meters": 5.0
    },
    "region": "NORWAY",
    "water_body": "FJORD"
  },
  "water_quality": {
    "temperature_celsius": 12.340,
    "dissolved_oxygen_mg_l": 9.120,
    "oxygen_saturation_percent": 91.500,
    "salinity_ppt": 34.210,
    "ph": 7.850,
    "turbidity_ntu": 1.230,
    "chlorophyll_a_ug_l": 2.140
  },
  "biological": {
    "fish_count_estimate": 4800,
    "avg_biomass_kg": 3.850,
    "feeding_rate_kg_per_hour": 42.500,
    "lice_count_per_fish": 0.120,
    "mortality_count_24h": 3
  },
  "environment": {
    "current_speed_m_s": 0.230,
    "current_direction_degrees": 275,
    "algae_bloom_detected": false,
    "algae_bloom_type": null
  },
  "alerts": [
    {
      "alert_code": "O2_LOW_WARNING",
      "severity": "WARNING",
      "threshold_breached": 9.0,
      "current_value": 9.120,
      "message": "Dissolved oxygen approaching lower threshold"
    }
  ],
  "data_quality": {
    "signal_strength_dbm": -62,
    "battery_level_percent": 87,
    "sensor_status": "NOMINAL",
    "missing_fields": []
  }
}
```

### 3.2 Field Reference Table

#### Envelope

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `schema_version` | `string` | ✅ | Semantic version of this schema |
| `event_id` | `string (UUIDv4)` | ✅ | Globally unique event identifier |
| `event_type` | `enum` | ✅ | Always `SENSOR_TELEMETRY` for this schema |
| `timestamp` | `string (ISO 8601)` | ✅ | Sensor capture time in UTC |

#### `producer` Object

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `device_id` | `string` | ✅ | Format: `SENSOR-{COUNTRY}-{FARM_ID}-{CAGE_ID}-{UNIT}` |
| `device_type` | `enum` | ✅ | `MULTIPARAMETER_PROBE`, `CAMERA_UNIT`, `FEEDING_SENSOR` |
| `firmware_version` | `string` | ✅ | Semver string |
| `manufacturer` | `string` | ✅ | Hardware vendor name |

#### `location` Object

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `farm_id` | `string` | ✅ | Unique farm identifier. **Kafka partition key** |
| `farm_name` | `string` | ✅ | Human-readable farm name |
| `cage_id` | `string` | ✅ | Specific pen/cage within the farm |
| `coordinates.latitude` | `float` | ✅ | WGS84 decimal degrees |
| `coordinates.longitude` | `float` | ✅ | WGS84 decimal degrees |
| `coordinates.depth_meters` | `float` | ✅ | Sensor deployment depth |
| `region` | `enum` | ✅ | `NORWAY`, `SCOTLAND`, `CHILE`, `CANADA`, `FAROE_ISLANDS` |
| `water_body` | `enum` | ✅ | `FJORD`, `OPEN_SEA`, `COASTAL`, `INLAND_LAKE` |

#### `water_quality` Object — Critical Thresholds

| Field | Type | Unit | Min Alert | Max Alert | Description |
|-------|------|------|-----------|-----------|-------------|
| `temperature_celsius` | `float` | °C | — | `18.0` | Water temperature. >18°C: High lice risk |
| `dissolved_oxygen_mg_l` | `float` | mg/L | `7.0` | — | <7.0: Immediate stress, <5.0: Mass mortality risk |
| `oxygen_saturation_percent` | `float` | % | `70.0` | `120.0` | Derived from temp + DO |
| `salinity_ppt` | `float` | ppt | `28.0` | `36.0` | Parts per thousand |
| `ph` | `float` | pH | `7.0` | `8.5` | Acceptable marine range |
| `turbidity_ntu` | `float` | NTU | — | `10.0` | Water clarity |
| `chlorophyll_a_ug_l` | `float` | μg/L | — | `10.0` | Algae bloom proxy indicator |

#### `biological` Object

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `fish_count_estimate` | `integer` | count | Estimated via sonar/camera |
| `avg_biomass_kg` | `float` | kg | Average individual fish weight |
| `feeding_rate_kg_per_hour` | `float` | kg/h | Active feed dispensed |
| `lice_count_per_fish` | `float` | count | Sea lice per fish. Regulatory limit: `0.5` |
| `mortality_count_24h` | `integer` | count | Dead fish removed in last 24 hours |

#### `alerts` Array

| Field | Type | Values | Description |
|-------|------|--------|-------------|
| `alert_code` | `enum` | See table below | Machine-readable alert identifier |
| `severity` | `enum` | `INFO`, `WARNING`, `CRITICAL` | Impact level for triage |
| `threshold_breached` | `float` | — | The configured limit that was crossed |
| `current_value` | `float` | — | The actual reading |
| `message` | `string` | — | Human-readable description |

**Alert Code Reference:**

| Alert Code | Severity | Trigger Condition |
|------------|----------|-------------------|
| `O2_LOW_WARNING` | `WARNING` | DO < 9.0 mg/L |
| `O2_CRITICAL` | `CRITICAL` | DO < 7.0 mg/L |
| `TEMP_HIGH_WARNING` | `WARNING` | Temp > 16.0°C |
| `TEMP_CRITICAL` | `CRITICAL` | Temp > 18.0°C |
| `LICE_THRESHOLD_BREACH` | `CRITICAL` | Lice > 0.5 per fish |
| `ALGAE_BLOOM_DETECTED` | `WARNING` | `chlorophyll_a > 10.0` or `algae_bloom_detected: true` |
| `HIGH_MORTALITY` | `CRITICAL` | Mortality > 50 fish/24h |
| `SENSOR_FAULT` | `WARNING` | Any sensor returning null for > 3 cycles |

---

## 4. Oslo Fish Market Data Event

**Kafka Topic:** `market.oslo.fish.prices`
**Schema File:** `/schemas/market_data_event.schema.json`
**Partition Key:** `species` + `product_form` (composite)
**Expected Frequency:** Every **5 minutes** during market hours (09:00–17:00 CET)

### 4.1 Full Example Payload

```json
{
  "schema_version": "1.0.0",
  "event_id": "f7e3b1a9-2c4d-4f8e-a6b5-9d0e1f2a3b4c",
  "event_type": "MARKET_PRICE_TICK",
  "timestamp": "2026-03-03T11:15:00+01:00",
  "source": {
    "provider": "FISH_POOL_OSLO",
    "feed_type": "SPOT",
    "market_session": "CONTINUOUS",
    "session_date": "2026-03-03"
  },
  "instrument": {
    "species": "ATLANTIC_SALMON",
    "scientific_name": "Salmo salar",
    "product_form": "FRESH_HOG",
    "weight_class_kg": "4-5",
    "origin_country": "NO",
    "certification": ["ASC", "GLOBAL_GAP"]
  },
  "pricing": {
    "currency": "NOK",
    "spot_price_per_kg": 87.5000,
    "bid_price_per_kg": 87.2500,
    "ask_price_per_kg": 87.7500,
    "spread_per_kg": 0.5000,
    "daily_open_price": 86.0000,
    "daily_high_price": 88.2500,
    "daily_low_price": 85.5000,
    "previous_close_price": 86.5000,
    "price_change_pct": 1.1561,
    "volume_traded_kg": 142500.000,
    "volume_traded_nok": 12468750.0000
  },
  "futures": {
    "available": true,
    "contracts": [
      {
        "contract_month": "2026-04",
        "settlement_price_per_kg": 89.0000,
        "open_interest_kg": 250000
      },
      {
        "contract_month": "2026-05",
        "settlement_price_per_kg": 91.5000,
        "open_interest_kg": 180000
      }
    ]
  },
  "market_sentiment": {
    "analyst_consensus": "NEUTRAL",
    "supply_pressure_index": 0.620,
    "demand_pressure_index": 0.480,
    "volatility_index_30d": 0.142,
    "news_sentiment_score": -0.120
  },
  "regulatory": {
    "weekly_quota_remaining_tons": 1250.500,
    "quota_utilization_pct": 62.300,
    "active_export_restrictions": []
  }
}
```

### 4.2 Field Reference Table

#### `source` Object

| Field | Type | Values | Description |
|-------|------|--------|-------------|
| `provider` | `enum` | `FISH_POOL_OSLO`, `NASDAQ_COMMODITIES`, `SINTEF_FEED` | Data origin |
| `feed_type` | `enum` | `SPOT`, `FUTURES`, `AUCTION` | Type of price data |
| `market_session` | `enum` | `PRE_MARKET`, `CONTINUOUS`, `CLOSING`, `POST_MARKET` | Current trading phase |

#### `instrument` Object

| Field | Type | Values | Description |
|-------|------|--------|-------------|
| `species` | `enum` | `ATLANTIC_SALMON`, `BLUEFIN_TUNA`, `PACIFIC_SALMON`, `RAINBOW_TROUT`, `ARCTIC_CHAR` | Fish species. Acts as partition key segment |
| `product_form` | `enum` | `FRESH_HOG`, `FRESH_GUTTED`, `FROZEN_WHOLE`, `SMOKED`, `FILLET_FRESH`, `FILLET_FROZEN` | Processing state |
| `weight_class_kg` | `string` | `1-2`, `2-3`, `3-4`, `4-5`, `5-6`, `6+` | Standard market weight classes |
| `origin_country` | `string` | ISO 3166-1 alpha-2 | `NO` = Norway, `CL` = Chile, etc. |
| `certification` | `array<string>` | `ASC`, `GLOBAL_GAP`, `MSC`, `ORGANIC_EU` | Quality certifications |

#### `pricing` Object

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `currency` | `enum` | — | `NOK`, `EUR`, `USD` |
| `spot_price_per_kg` | `float (4dp)` | currency/kg | Current spot price |
| `bid_price_per_kg` | `float (4dp)` | currency/kg | Best buy offer |
| `ask_price_per_kg` | `float (4dp)` | currency/kg | Best sell offer |
| `spread_per_kg` | `float (4dp)` | currency/kg | `ask - bid` |
| `daily_open_price` | `float (4dp)` | currency/kg | Opening price for the session |
| `daily_high_price` | `float (4dp)` | currency/kg | Session high |
| `daily_low_price` | `float (4dp)` | currency/kg | Session low |
| `previous_close_price` | `float (4dp)` | currency/kg | Previous session closing price |
| `price_change_pct` | `float (4dp)` | % | `((spot - prev_close) / prev_close) * 100` |
| `volume_traded_kg` | `float` | kg | Total kg traded in session so far |
| `volume_traded_nok` | `float` | NOK | Total NOK value traded |

#### `market_sentiment` Object

| Field | Type | Range | Description |
|-------|------|-------|-------------|
| `analyst_consensus` | `enum` | — | `STRONG_BUY`, `BUY`, `NEUTRAL`, `SELL`, `STRONG_SELL` |
| `supply_pressure_index` | `float` | 0.0–1.0 | Higher = more supply, downward price pressure |
| `demand_pressure_index` | `float` | 0.0–1.0 | Higher = more demand, upward price pressure |
| `volatility_index_30d` | `float` | 0.0–1.0 | Normalized 30-day price volatility |
| `news_sentiment_score` | `float` | -1.0–1.0 | Computed by NLP on recent news. -1 = very negative, +1 = very positive |

---

## 5. Envelope Metadata Fields (Common)

All events share these top-level fields:

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | `string` | Semver. Consumers must fail fast on unsupported versions |
| `event_id` | `string (UUIDv4)` | Idempotency key. Consumers **must** deduplicate on this |
| `event_type` | `string (enum)` | Machine-readable type discriminator |
| `timestamp` | `string (ISO 8601)` | Event creation time at the **source** |

---

## 6. Versioning & Evolution Rules

| Rule | Policy |
|------|--------|
| **Adding a new optional field** | Allowed without version bump. Consumers must ignore unknown fields |
| **Renaming a field** | **BREAKING** — requires major version bump (v1 → v2) + deprecation period |
| **Changing a field type** | **BREAKING** — requires major version bump |
| **Removing a field** | **BREAKING** — mark as `deprecated` for one full minor version first |
| **Adding a new enum value** | Minor version bump required. Consumers must handle unknown enums gracefully |
| **Schema Registry subject** | Format: `{topic-name}-value`. Schema ID embedded in message header |
