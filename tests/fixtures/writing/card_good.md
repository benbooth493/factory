## Problem
A 12 MB PDF spins forever on the receipts page. The 413 is never handled.

## Steps to reproduce
1. Sign in
2. Open Receipts
3. Drop a 12 MB PDF on the drop zone

## Acceptance criteria
- [ ] The size message shows within 1 s of the drop
- [ ] No request over 10 MB leaves the browser
