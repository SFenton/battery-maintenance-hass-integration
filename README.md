# Battery Maintenance

Battery Maintenance is a Home Assistant custom integration that maps selected
battery-percentage entities to one of two household actions:

- **Replace battery**
- **Charge battery**

It creates or updates one unassigned DoneTick task per physical device, keeps
the latest percentage in the task title and description, preserves the
original due date, and completes the task when the battery reports a healthy
percentage.

## Requirements

- Home Assistant 2026.7.0 or newer
- HACS 2.0.0 or newer
- The `donetick` custom integration with these services:
  - `donetick.create_task_form`
  - `donetick.update_task_form`
- `todo.all_tasks_internal` from the DoneTick integration

DoneTick must use **Username & Password (Full Features)** authentication. Its
API-key mode cannot update the managed task form.

## Installation

1. Add this repository to HACS as a custom **Integration** repository.
2. Download **Battery Maintenance**.
3. Restart Home Assistant.
4. Open **Settings → Devices & services → Add integration**.
5. Search for **Battery Maintenance**.

## Configuration

The integration has one configuration entry with:

- the DoneTick instance that owns managed tasks;
- entities whose batteries should be replaced;
- entities whose batteries should be charged;
- entities that are still Unknown or intentionally ignored;
- the low-battery threshold;
- the recovery threshold;
- the daily fallback reconciliation time.

An entity cannot appear in more than one list. Multiple actionable entities
that resolve to the same physical device are also rejected.

One Battery Maintenance entry manages one full-featured DoneTick entry.

New enabled percentage battery sensors are automatically added to **Unknown**.
Battery Maintenance creates one DoneTick task assigned to Stephen with
instructions to move the entity to Replace or Charge, or leave it Unknown to
ignore it. Unknown is the intentional Ignore bucket. Completing that task
records the review and does not create another categorization task for the
same entity.

Review task names preserve explicit entity names and otherwise include Home
Assistant's device name, such as `Mustang Mach-E Battery (12V)`. If no useful
device or entity name is available, Battery Maintenance waits instead of
creating an opaque raw-ID task. Renamed entities are recovered through their
stable integration identity. Reviews are automatically retired when their
entity has already been categorized or can no longer be configured; any
matching integration-owned DoneTick task is completed and the retained review
record prevents rediscovery. Categorization instructions remain in the task
description.

Managed task titles use the Home Assistant device name. Keep those names
household-facing and functional, such as `Front Yard Faucet` rather than only
`Front Yard`, so task and notification surfaces identify the device clearly.

## Task lifecycle

1. A numeric mapped battery at or below the low threshold opens one DoneTick
   task due at 5:00 PM on the detection date.
2. Every mapped battery state change triggers immediate reconciliation.
3. While the task remains active, its title and description update to the
   latest percentage without moving the deadline or changing assignment.
4. At or above the recovery threshold, Battery Maintenance completes the active
   DoneTick task and clears the episode.
5. Manually completing or deleting the task suppresses another task until the
   battery reaches the recovery threshold. A later low reading may then create
   a new task.
6. Unknown and unavailable readings never create or clear an episode.

## Manual reconciliation

Use either:

- the **Sync tasks** button created by the integration; or
- the `battery_maintenance.sync` action.

## Development

```bash
uv sync --group dev
uv run ruff check .
uv run pytest
```
