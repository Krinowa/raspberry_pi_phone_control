# Mock Withdraw Bot API v2

Local-only test implementation of the documented Withdraw Bot API v2 HTTP and
Pusher-compatible realtime flow. It is a test tool, not evidence of compatibility
with the real service and never performs a payment.

## Install and run

```bash
cd /Users/weizhe/Documents/phone_controller/mock_withdraw_api
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

- HTTP and mock UI: <http://127.0.0.1:9100/mock>
- WebSocket: `ws://127.0.0.1:9101/app/mock-withdraw-v2`
- API prefix: `http://127.0.0.1:9100/api/admin-withdraw/v2`
- Debug state: <http://127.0.0.1:9100/mock/state>

The HTTP and WebSocket listeners bind to `127.0.0.1` only.

## Production dashboard configuration

The production client remains HTTPS/WSS-only by default. Explicitly enable its
localhost test hook when running this mock:

```bash
export WITHDRAW_API_BASE_URL=http://127.0.0.1:9100
export WITHDRAW_ALLOW_INSECURE_LOCALHOST=1
```

The insecure hook accepts only `http://127.0.0.1` or `http://localhost`; it does
not permit plaintext remote hosts.

Use these credentials in each phone's **Withdraw Account → Login & Assign** UI.
The password for every account is `password123`.

| Username     | Operator ID | Payment records |
| ------------ | ----------: | --------------- |
| `operator_a` |         101 | `5`             |
| `operator_b` |         102 | `5`, `2b`       |
| `operator_c` |         103 | `2b`            |
| `operator_d` |         104 | `9`             |

The fixed TOTP secret is `JBSWY3DPEHPK3PXP`. The mock validates the current
RFC 6238 code generated from that secret (with a one-step clock window). This is
used instead of a constant `123456` because the production client generates a
real TOTP before login.

## Seed data

Pending withdrawals:

|     ID | Transaction | Record | Final amount | Purpose                      |
| -----: | ----------- | ------ | -----------: | ---------------------------- |
| 935402 | `TX9F3K2`   | `5`    |     RM 50.00 | normal payment/cancel        |
| 935403 | `TX9F3K3`   | `2b`   |    RM 120.00 | record visibility/confirm    |
| 935404 | `TX9F3K4`   | `5`    |   RM 9999.00 | insufficient balance/release |
| 935405 | `TX9F3K5`   | `5`    |     RM 25.00 | priority 1                   |
| 935406 | `TX9F3K6`   | `9`    |     RM 80.00 | isolated queue               |

History:

|     ID | Transaction        | State                            |
| -----: | ------------------ | -------------------------------- |
| 935450 | `TX-CONFIRMED-001` | `PROOF_PENDING` for operator 101 |
| 935451 | `TX-CANCELLED-001` | `CANCELLED`                      |

Pending ordering is priority descending, then `created_at`, then ID. Therefore
operator A initially receives `935405, 935402, 935404`.

Visibility matrix:

| Withdrawal record |  A  |  B  |  C  |  D  |
| ----------------- | :-: | :-: | :-: | :-: |
| `5`               |  ✓  |  ✓  |     |     |
| `2b`              |     |  ✓  |  ✓  |     |
| `9`               |     |     |     |  ✓  |

## Implemented documented HTTP endpoints

- `POST /api/admin-withdraw/v2/login`
- `GET /api/admin-withdraw/v2/me`
- `POST /api/admin-withdraw/v2/logout`
- `POST /api/admin-withdraw/v2/broadcasting/auth`
- `POST /api/admin-withdraw/v2/withdrawals/{id}/confirm`
- `GET /api/admin-withdraw/v2/withdrawals/my-approved`

Login, me, logout, confirmation, and approved history use the standard
`status/message/data` envelope. Broadcasting authorization returns the Pusher
`auth` response directly. Confirmation accepts `proof` and `proofs[]` multipart
uploads and stores them in `proofs/`.

## Realtime protocol

Implemented Pusher messages:

- `pusher:connection_established`
- `pusher:subscribe` / `pusher:unsubscribe`
- `pusher_internal:subscription_succeeded`
- `pusher:ping` / `pusher:pong`
- private session and queue authorization
- presence online authorization and subscription metadata

Channels follow these documented forms:

- `private-apk-withdraw.session.<session-id>`
- `private-apk-withdraw.queue.<payment-record>`
- `presence-apk-withdraw.online`

Client actions on the session channel:

- `client-pending`
- `client-my-locked`
- `client-lock`
- `client-renew-lock`
- `client-release`
- `client-cancel`

Every reply is `action.result` on the same session channel and preserves
`request_id`. Queue mutations emit `queue.changed` on each affected queue
channel. One physical socket may authorize several session channels using
different Bearer tokens.

## Mock control endpoints

These are test-only and are not part of API v2:

- `GET /mock/state`
- `POST /mock/reset` — restores all users/data, clears tokens, locks, logs,
  scenarios, time offset, and uploaded proof files; connected sockets close
- `POST /mock/scenario` with `{"scenario":"lock_423"}`
- `POST /mock/withdrawals` with
  `{"payment_record":"5","final_amount":"75.00","priority":0,`
  `"bank_name":"Mock Bank","account_number":"0000000001",`
  `"account_name":"Mock User"}`; ID, order ID, transaction ID, and timestamp
  are generated automatically
- `POST /mock/withdrawals/reset` with `{}` — clears every pending, locked, and
  historical withdrawal while preserving active mock sessions
- `POST /mock/emit-queue-changed`
- `POST /mock/disconnect`
- `POST /mock/expire-session`
- `POST /mock/advance-time` with `{"seconds":601}`
- `POST /mock/lock-expire`

`/mock/state` includes sessions, socket IDs, subscriptions, channel authorizers,
withdrawal states, lock owners/expiry, proofs, actions, results, and queue events.
The mock UI exposes these as separate **Reset to Seed Data** and
**Clear Withdrawals** controls. Clearing withdrawals remains empty across page
refreshes; only reset or process startup restores the seed data.

## Failure scenarios

Available through `/mock/scenario` or the mock UI:

```text
normal
lock_404 lock_409 lock_423
lock_timeout_before_apply lock_timeout_after_apply
renew_timeout_before_apply renew_timeout_after_apply renew_409
release_timeout_before_apply release_timeout_after_apply
cancel_timeout_before_apply cancel_timeout_after_apply
confirm_timeout_before_apply confirm_timeout_after_apply
delayed_action_result duplicate_action_result duplicate_queue_changed
out_of_order_action_result socket_disconnect session_expired
short_lock_ttl auth_failure
```

`timeout_before_apply` performs no mutation and sends no realtime result.
`timeout_after_apply` performs the mutation but sends no realtime result. HTTP
confirm timeout scenarios hold the response beyond the production client's read
timeout; after-apply writes approved history first.

## Manual test recipes

- Shared socket: assign operator A to Phone A and operator B to Phone B, then
  inspect `/mock/state`; `physical_websocket_count` is 1 and both session
  channels show their own authorizing tokens.
- Release: lock 935404, let the dashboard reject it for insufficient balance,
  and verify it returns to pending with the same ID and transaction ID.
- Cancel: lock 935402 and select **Invalid account number** in the dashboard;
  verify `cancel_reason` and permanent removal from pending.
- Confirm: lock 935403 as operator B or C and upload a fake JPG/PNG/PDF receipt;
  verify `PROOF_PENDING`, saved proof metadata, and `my-approved` visibility.
- Ambiguous confirm: select `confirm_timeout_after_apply`; the client times out,
  then can reconcile the exact ID and transaction through `my-approved`.
- Reconnect: lock an item, call `/mock/disconnect`, and verify resubscription,
  `client-my-locked`, and no duplicate payment.
- Repeat: call `/mock/reset` before the next scenario.

## Tests

```bash
cd /Users/weizhe/Documents/phone_controller/mock_withdraw_api
.venv/bin/python -m unittest discover -s tests -v
python -m compileall .
```

The integration tests import the real production `WithdrawClient`,
`SharedPusherTransport`, and `PusherSocket`; no phone, camera, Bluetooth, bank
application, or real funds are required.

## Known compatibility limits

This mock follows the supplied v2 protocol material, but real API access is still
required to verify real credentials/TOTP policy, exact undocumented validation
wording, production Soketi quirks, TLS behavior, real timing/rate limits, queue
ordering under load, receipt content validation, session edge cases, and whether
multi-Bearer authorization on one physical socket is enabled in production.

The production client currently logs in directly and does not call `GET /me`,
does not subscribe to the optional presence channel, uploads the singular
`proof` field, and exact-matches partially filtered `my-approved` results. The
mock supports the broader documented HTTP/presence/proof behavior without
changing those production choices.
