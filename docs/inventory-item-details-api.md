# API: Inventory Item Details — by State/Status at a Facility

**Owner:** SCM team · **Service:** SpaceManagementService (via Bolt Gateway)
**Status:** 🟢 Live in production. Shipped via SCM-1251 (SDK), SCM-1252 (service), SCM-1253 (gateway).
**Last updated:** 2026-06-18

---

## Overview

Returns full inventory-item details for a set of JPINs at a facility, filtered by inventory state + status. It is the detail counterpart of the existing `…/count/for-state-status-facility` endpoint: same request shape, but instead of just per-JPIN counts it returns each inventory item with its **listing selling price**, its **own creation time**, and its **origin item's id + creation time**.

Typical uses:
- **SELLABLE / active stock:** when each item was created, and when its origin item was created.
- **OUTWARD:** when the origin item of an outwarded item was created.

---

## Endpoint

```
POST https://bolt.jumbotail.com/api/space/product/details/for-state-status-facility
```

### Headers
| Header | Value |
|---|---|
| `userId` | caller email, e.g. `sripad.rao@jumbotail.com` |
| `orgId` | e.g. `ORGPROF-1304473228` |
| `Authorization` | standard gateway auth |
| `Content-Type` | `application/json` |

---

## Request body

| Field | Type | Required | Notes |
|---|---|---|---|
| `jpins` | string[] | ✅ | List of JPINs. |
| `facilityId` | string | ✅ | `FACIL-…` or `BZID-…`. |
| `inventoryItemStates` | string[] | ✅ | e.g. `SELLABLE`, `FULFILMENT`, `INWARDED`, `UNDER_TRANSFER`, `OUTWARDED`. |
| `inventoryItemStatuses` | string[] | ✅ | e.g. `ACTIVE`, `ONHOLD`, `EXHAUSTED`. |
| `createdTimeAfter` | long (epoch ms) | ⚠️ conditional | Optional for active states. **Mandatory when `OUTWARDED` is requested, and must be ≥ now − 2 days.** Only items created at/after this time are returned. |
| `maxResults` | int | optional | Caps rows returned. Must be **> 0** if provided. Omit for no cap. |

### Bounding rules (important)
- **Active states** (`SELLABLE`, `FULFILMENT`, `INWARDED`, `UNDER_TRANSFER`): return live stock only (`leftQty > 0`), naturally bounded. `createdTimeAfter` optional.
- **`OUTWARDED`** (delivered / exhausted, `leftQty = 0`): unbounded history, so a time window is **required** — pass `createdTimeAfter` within the last **2 days**. Requests with `OUTWARDED` and a missing/older `createdTimeAfter` are rejected with HTTP 400.

---

## Response

`200 OK`
```jsonc
{
  "success": true,
  "statusCode": 200,
  "data": [
    {
      "inventoryItem": {
        "inventoryItemId": "INVITM-2283292662",
        "jpin": "JPIN-1304362866",
        "productTitle": "…",
        "spaceBO": { "...": "space + facility details" },
        "lotId": "…",
        "listingId": "…",
        "initialQty": 40,
        "leftQty": 39,
        "inventoryItemState": "SELLABLE",
        "inventoryItemStatus": "ACTIVE"
        // … remaining InventoryItemBO fields
      },
      "listingSellingPrice": 8.60,                  // selling price of this item's listing (nullable)
      "inventoryItemCreatedTime": 1778217154917,    // when THIS item was created (epoch ms)
      "originInventoryItemId": "WITM-...",           // null when item has no origin (root inventory)
      "originInventoryItemCreatedTime": 1778100000000 // null when no origin
    }
  ],
  "error": null
}
```

### Field guide
| Field | Meaning |
|---|---|
| `inventoryItem` | The full inventory item (same `InventoryItemBO` returned by other space APIs). |
| `listingSellingPrice` | Selling price of the item's listing (from Lot Management). `null` if the listing has no selling price. |
| `inventoryItemCreatedTime` | When this inventory item row was created. |
| `originInventoryItemId` | The origin (parent-lineage) item's id. `null` for root items created directly (no transfer/split). |
| `originInventoryItemCreatedTime` | When the origin item was created. `null` when there is no origin. |

> **Note on `null` origins:** items created directly at a location (e.g. store inward) have no lineage, so `originInventoryItemId` and `originInventoryItemCreatedTime` are `null`. Items derived via transfer/split carry a populated origin.

---

## Examples

### 1) Active inventory (no time bound needed)
```bash
curl --location 'https://bolt.jumbotail.com/api/space/product/details/for-state-status-facility' \
--header 'userId: sripad.rao@jumbotail.com' \
--header 'orgId: ORGPROF-1304473228' \
--header 'Authorization;' \
--header 'Content-Type: application/json' \
--data '{
    "jpins": ["JPIN-1304362866", "JPIN-1304472659"],
    "facilityId": "FACIL-1441684058",
    "inventoryItemStates": ["SELLABLE", "FULFILMENT", "INWARDED", "UNDER_TRANSFER"],
    "inventoryItemStatuses": ["ACTIVE", "ONHOLD"]
}'
```

### 2) OUTWARDED (requires `createdTimeAfter` within 2 days)
```bash
curl --location 'https://bolt.jumbotail.com/api/space/product/details/for-state-status-facility' \
--header 'userId: sripad.rao@jumbotail.com' \
--header 'orgId: ORGPROF-1304473228' \
--header 'Authorization;' \
--header 'Content-Type: application/json' \
--data '{
    "jpins": ["JPIN-1304362866"],
    "facilityId": "FACIL-1441684058",
    "inventoryItemStates": ["OUTWARDED"],
    "inventoryItemStatuses": ["ACTIVE", "EXHAUSTED"],
    "createdTimeAfter": 1781616000000,
    "maxResults": 500
}'
```

---

## Errors
| HTTP | When |
|---|---|
| 400 | Missing/empty `jpins`, `facilityId`, `inventoryItemStates`, or `inventoryItemStatuses`. |
| 400 | `maxResults` ≤ 0. |
| 400 | `OUTWARDED` requested with `createdTimeAfter` null or older than 2 days. |

---

## Notes for callers
- Same request body as the existing count endpoint — easy to migrate.
- For large pulls, prefer setting `maxResults` and/or a tighter `createdTimeAfter`.
- Listing price is fetched live from Lot Management per call.
