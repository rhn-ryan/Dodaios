# Module: Core Event Bus (`dodaios.core.bus`)

## Identifier
`module:core:bus`

## Source Paths
- `dodaios/core/bus.py`

## Architectural Location
- Layer: L2 (Infrastructure)
- Package: `dodaios.core`
- Role: In-process async event bus

## Purpose
Decouples layers via pub/sub event infrastructure. L2 Speech publishes `UtteranceCaptured`, L3 Intent subscribes.

## Responsibilities
1. Async event bus with per-event-class topic subscriptions
2. Trace-ID-based correlation for debug replay
3. Delivers events to flight recorder when enabled
4. Can be swapped for Redis bus later for multi-process

## Important Classes

### `EventBus`
- **Path**: `dodaios/core/bus.py:45`
- **Methods**:
  - `subscribe(event_type, handler)` — Register handler for specific event type
  - `subscribe_all(handler)` — Register wildcard handler receiving ALL events
  - `unsubscribe_all(handler)` — Remove wildcard handler
  - `unsubscribe(event_type, handler)` — Remove typed handler
  - `publish(event)` — Deliver event to all matching subscribers in parallel
- **Key behavior**: Wildcard handlers have 5.0s timeout; typed handlers are uncapped

### `get_default_bus()`
- **Path**: `dodaios/core/bus.py:139`
- **Returns**: Singleton `EventBus` instance

### `reset_default_bus()`
- **Path**: `dodaios/core/bus.py:147`
- **Purpose**: Test-only reset

## Internal Dependencies
- `dodaios.core.events` (Event base class)
- `loguru` (logging)

## External Dependencies
- `asyncio` (stdlib)
- `collections.defaultdict` (stdlib)

## Upstream Dependencies
- `dodaios.core.events` — Event type definitions
- `dodaios.speech.*` — publishes `UtteranceCaptured`
- `dodaios.brain.*` — publishes brain events

## Downstream Consumers
- WebSocket forwarders (subscribe_all)
- Flight recorder (subscribe_all)
- Channel adapters (subscribe_all)
- Metrics collectors (subscribe_all)

## Events and Interfaces
- Publishes/Consumes: `dodaios.core.events.Event` base class
- Subscribers register via `subscribe(type, handler)` or `subscribe_all(handler)`

## Shared State
- `_default_bus` module-global singleton
- `_subscribers` dict keyed by event type
- `_wildcard_subscribers` list

## Database Interactions
- None (in-memory only)

## Filesystem Interactions
- None

## Network and Subprocess Interactions
- None (pure in-process)

## Configuration Dependencies
- None

## Execution Lifecycle
1. Created on first `get_default_bus()` call
2. Subscribers register via `subscribe()` or `subscribe_all()`
3. Events published via `publish()` trigger parallel dispatch
4. Reset via `reset_default_bus()` in tests

## Error Handling
- Subscriber exceptions logged but not propagated
- Wildcard handler timeout (5s) with warning log
- Typed handler exceptions logged with trace_id

## Failure Modes
- Wedged wildcard handler (e.g. stalled WebSocket) abandoned after 5s timeout
- Subscriber errors silently logged

## Existing Tests
- See `tests/unit/core/test_bus.py` (if exists)

## Known Limitations
- In-process only; cannot span processes
- No persistence

## Extension Points
- Can be swapped for Redis bus for multi-process deployments
- Wildcard timeout configurable via `_WILDCARD_HANDLER_TIMEOUT_S`

## Evidence
- **VERIFIED**: Bus implementation at `dodaios/core/bus.py:45-151`
- **VERIFIED**: Timeout constant at `dodaios/core/bus.py:42`
- **INFERRED**: Used by speech and brain layers (referenced in docstring)

## Confidence
- **VERIFIED**

## Last Verified Commit
`6e16a87`