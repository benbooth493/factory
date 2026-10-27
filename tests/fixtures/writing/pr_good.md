Closes #790

## Why
Uploads over 10 MB spin forever because the client ignores the 413.

## What changed
- Client checks the size before upload and shows "File too large (max 10 MB)"
- One limit constant shared by client and server

## Acceptance criteria
- [x] A 12 MB file shows the size message within 1 s: `e2e/upload-size.spec.ts:14`
- [x] No request over 10 MB is sent: same spec, asserts 0 calls

## Risk
Low. Revert restores the old behaviour.
