# Bench workflow improvements

## Evidence and scope

- The current `main` commit matches the fetched `origin/main` commit, `37c78c8`.
- A live bench session on a handheld PC exposed the failures below. This report describes changes; it does not implement them.
- Keep proprietary board files, part lists, and net lists out of tracked files.

## P0: Match the snapshot to the monitor preview

**Observed failure:** The user saw four coils in one direction in the monitor preview. The agent saw a different direction in `phone_snapshot`.

**Cause:** `ui/static/app.js` turns the phone screen canvas with `viewRotation()`. `Services.phone_snapshot()` applies only `SnapshotOrientation` flips to the phone still. The phone app also rotates its still through `/v1/rotation`. The two paths have no shared final orientation contract.

**Change:** Define one effective camera image transform for the preview and `phone_snapshot`. Include the saved `screen_rotation` choice, the phone status `rotation_degrees`, the phone still rotation, and the saved flips. Compute the remaining quarter turn between the screen camera image and the still. Apply that turn to the still before its flips and scale. Do not apply the phone still rotation twice. Return the final turn and flips in `SnapshotInfo`. Use the same transform for saved images, `multimeter_read(source="phone")`, focus points, overlays, and board photo coordinates. Update `docs/phone-api.md` before changes to both sides of the contract.

**Acceptance:** Put one asymmetric, open-license marker beside four test shapes. For each monitor choice of 0°, 90°, 180°, and 270°, take a fresh `phone_snapshot`. Check the marker direction and the order of all four shapes against the monitor preview. Repeat with automatic rotation and each flip. Check that image dimensions, focus points, and overlay boxes follow the same turn. Run the test after a phone rotation and after an app restart.

## P1: Check meter mode and unit before a conclusion

**Observed failure:** The user reported about 443 kΩ in resistance mode with both power sources removed. `multimeter_read` classified similar digits as volts with low confidence.

**Current support:** `MultimeterReading` already returns `display_text`, `mode`, `unit`, `confidence`, and `notes`. The prompt names the Proster T21D when its model setting is set. It tells the model to use LCD symbols before the dial. The schema still accepts any unit string and does not check the unit against the mode.

**Change:** Add an explicit unknown unit state. Keep the exact LCD text separate from the numeric value. Check mode and unit pairs after model output. If the pair conflicts, return an uncertain result and the image evidence; do not report a numeric electrical conclusion. Ask for a new frame that shows the LCD symbols and dial together. Record the expected mode from the current test as context, not as proof of the LCD mode. Require user confirmation of the physical meter mode when symbols remain unclear.

**Acceptance:** Test clear Ω, kΩ, MΩ, V, and `OL` displays. Test obscured symbols and a model result of `443 V` during a resistance test. The last two cases must give an unknown or disputed unit and no confirmed numeric measurement. Keep the exact model image available through `include_image` and the monitor log.

## P1: Tie statements to the exact frame

**Observed failure:** An agent said that the probes left the board. A fresh photo still showed a probe on the part.

**Current support:** `phone_snapshot` stores the last image for focus and highlights. The monitor counts snapshots. The webcam stream has an internal frame sequence and capture time. The tool results do not expose one common capture ID and time for evidence statements.

**Change:** Return a capture ID and UTC capture time with every phone snapshot and meter result. Give `multimeter_read` the ID of the exact image sent to the model. Return that image when the caller requests it. Tie each visual statement about probes, contact, or part position to a current `phone_snapshot` ID. If the board or phone moves, require a new photo before a position statement.

**Acceptance:** A meter result and its returned image have the same capture ID. Two calls have different IDs. A statement about probe position names a photo ID from the current scene. A scene change makes the old ID invalid for new position claims.

## P1: Check physical part identity

**Observed failure:** Similar coils led to a wrong physical part name. The agent treated boardview location data as proof from a photo, then corrected the name.

**Current support:** `board_register_photo` maps at least four known parts to the visible board side. `board_locate_in_photo` returns estimated positions and can highlight them. `board_match_marking` handles visible text and candidate matches. Scene checks reject stale registrations.

**Change:** Keep a physical identity state: visible marking, candidate, or confirmed part. Require a fresh photo, a visible landmark or marking, and a valid registration before a confirmed refdes claim. Use `board_locate_in_photo` to narrow candidates. Use `board_match_marking` for text that the photo shows. If adjacent parts remain alike, state the candidates and ask for a closer photo. Never promote a boardview estimate to a visual fact.

**Acceptance:** A photo of four similar unmarked coils gives candidates, not one confirmed refdes. A clear marking plus a valid registration can confirm one part. A moved board or an opposite-side target removes that confirmation.

## P2: Keep a compact bench state

**Observed failure:** The ignored `instructions.md` still names a completed measurement as the next step.

**Change:** Keep one small session record with the power state, meter mode, probe contact, confirmed measurements, part candidates, photo IDs, and the next uncompleted step. Give each measurement a source ID, time, unit, and confidence state. Keep estimated part names separate from confirmed measurements. Mark a step complete when its evidence enters the record. On the next `bench_instructions` call, show the current step after the user instructions. Keep this record local and outside tracked reports.

**Safety check:** Before resistance, continuity, or diode tests, tell the user to disconnect the charger and battery or bench supply. Wait for confirmation. Check residual voltage with the meter in voltage mode before a resistance test. If voltage remains, stop the resistance test. After any probe short, stop and check the power state again.

**Acceptance:** A completed measurement no longer appears as the next step. A low-confidence meter result cannot enter confirmed measurements. A resistance step cannot start until the record shows power isolation, user confirmation, and a safe residual-voltage result. After a probe short, the record returns to the power check.

**Local continuity update:** Update the ignored `instructions.md` to mark the completed measurement as complete. Record the next uncompleted step and the power check there. Do not copy board file data into it through this report.
