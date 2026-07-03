# CV Descriptor

`cv_descriptor` is the pilot skill for the browser-side AdaOS CV runtime. The skill stores visual-object descriptors: a browser model signature, embedding vector, thumbnail, title, and description. The client CV runtime is expected to capture vectors in setup mode and match a small descriptor list in work mode.

## Target Architecture

The browser client owns camera access, model loading, inference, overlays, and low-latency matching. The skill owns durable descriptor metadata and exposes the webui/control contract.

Core runtime components:

- `CvRuntimeService` in the AdaOS client: starts/stops camera sessions, loads model adapters, runs inference, emits diagnostics, and compares embeddings against a small target list.
- `media.cvCamera` widget: visible camera preview, match overlay, setup capture controls, and diagnostics.
- `host.cvRuntime` widget: headless session host for pages where another surface is shown instead of camera preview.
- `cv_descriptor` skill: persists descriptors and projects compact Yjs state; raw vectors are returned through `cv_descriptor_get_targets` instead of public descriptor lists.

Skill-owned use cases:

- The skill defines `session.useCase`: `id`, `kind`, `profile`, `camera`, `pipeline`, `matching`, `targets`, `sinks`, and optional `ui` hints.
- The client treats `useCase` as the scenario contract. Runtime profiles only provide defaults; explicit skill session fields win.
- Supported profile defaults are `low_power`, `balanced`, and `quality`. `cv_descriptor` uses `low_power` for phone testing.
- `cv_descriptor` currently requests `320x240`, camera `frameRate` max `5`, and runtime `targetFps` `4` to reduce heat while preserving object identification.

Session flow:

1. Setup mode starts `cv_descriptor.setup`.
2. The browser loads the selected CV model and captures an embedding when the user selects an object.
3. The browser opens a descriptor edit modal with the captured thumbnail, then calls `cv_descriptor_save_descriptor` with vector, thumbnail, title/description, and model signature.
4. Work mode starts `cv_descriptor.work`.
5. The browser calls `cv_descriptor_get_targets`, compares embeddings locally, and emits `cv_descriptor.match` events on enter/update/exit.
6. The skill records current match and diagnostics through `cv_descriptor_record_runtime_event`.

Model rules:

- Every stored vector is tied to `model_signature`.
- Work mode only returns targets compatible with the active model signature.
- Changing the model reconfigures the browser session and makes incompatible descriptors inactive for matching until recaptured or migrated.

## Current Implementation

- `skill.yaml` declares tools, data projections, event subscriptions, and stream budgets.
- `handlers/main.py` provides durable JSON state, descriptor CRUD tools, target export, runtime command state, Yjs projection, and bounded stream events.
- The AdaOS client contains the first `CvRuntimeService` edition with camera lifecycle, TensorFlow.js MobileNetV2 trial adapter using an explicit Google Storage model URL, browser-frame fallback adapter, browser-side target matching, visible `media.cvCamera`, and headless `host.cvRuntime`.
- `webui.json` uses `media.cvCamera` in setup/work modals and keeps generic status/list/debug widgets around it.
- CV session diagnostics include the active use-case summary and actual browser camera track settings when available.

## Phone Smoke Check 2026-07-02

- Camera start: verified on phone.
- Permission/HTTPS: no blocking issue observed.
- Preview: visible.
- Work overlay: visible; runtime shows `running`, about 10 fps.
- Select/descriptors: not observed in the previous build.
- Model state in the previous build: browser-frame embedding placeholder only; no real model load yet.

## Roadmap Checklist

- [x] Create AdaOS skill skeleton in `.adaos/workspace/skills/cv_descriptor`.
- [x] Persist descriptor records with vector, thumbnail, title, description, model id, and model signature.
- [x] Expose `cv_descriptor_get_targets` for browser-side small-list matching.
- [x] Project compact public state to `data/cv_descriptor/current`, `data/cv_descriptor/descriptors`, and `data/cv_descriptor/runtime`.
- [x] Add setup/work webui surfaces using existing widgets.
- [x] Add client `CvRuntimeService`.
- [x] Add model adapter registry with stable model signatures and reinitialization on model change.
- [x] Add `media.cvCamera` visible widget.
- [x] Add `host.cvRuntime` headless widget.
- [x] Add browser-side cosine/L2 matcher with debounce and hysteresis.
- [x] Wire browser events to `cv_descriptor.runtime.event`, `cv_descriptor.match`, and `cv_descriptor.runtime.diagnostics`.
- [x] Replace setup placeholder controls with actual camera preview and Select button.
- [x] Add post-Select descriptor edit modal with thumbnail, title, and description.
- [x] Add runtime contract copy action for phone debugging.
- [x] Add TensorFlow.js MobileNetV2 trial model adapter.
- [x] Add skill-owned `useCase` contract with runtime load profiles.
- [x] Move `cv_descriptor` phone profile to low-power camera/FPS defaults.
- [ ] Add list-item edit UI for existing descriptor title/description.
- [ ] Add end-to-end browser test with synthetic camera/model adapter.
