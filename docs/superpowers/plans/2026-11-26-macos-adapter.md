# macOS runtime implementation plan

> For implementation: use the existing M1/M2 tests and a red → green macOS test cycle.

**Goal:** Port the existing managed agy session and manual account relay to macOS without changing the Windows runtime or upstream HUD.

**Architecture:** The runtime factory selects a Darwin implementation around the same process, auth-probe and launch contract. macOS runs the child in the foreground of the invoking terminal, verifies its first three seconds under the switch lock, then waits outside that lock. No AppleScript, new Terminal window, or third-party dependency.

**Constraints:** Python 3.12; release only as a preview until native macOS and real OAuth/HUD checks pass. Platform-specific state remains local. Branch `feat/macos`; local tag `macos-base` points to the Windows starting commit.

- [x] Write tests for same-user agy-only enumeration, timestamp precision, unsupported/access-denied handling, non-terminal launch rejection, and foreground wait after lock release.
- [x] Add native tests using only harmless process and filesystem fixtures: repeatable microsecond PID identity, contention and release of the same lock across processes, and pseudo-terminal attachment of a fake executable.
- [x] Implement `MacAgyRuntime` and `default_runtime()`. Use `/bin/ps` for process names and libproc `PROC_PIDTBSDINFO` for birth timestamps; keep matching but unreadable processes in conflict detection. Use one per-user `flock` path, independent of `AGY_POOL_HOME` and `AGY_POOL_DB`.
- [x] Add a shell launcher and document quick start, same-terminal interface, Apple runtime test command, and manual `start` → `switch` smoke test.
- [x] Run the complete existing suite plus macOS fixtures on the available Windows host. Native process identity, terminal attachment and POSIX lock tests are skipped here. Do not push or change `agy-hud`.
- [ ] On an actual Mac, run the native tests and the manual HUD / OAuth acceptance checklist.
