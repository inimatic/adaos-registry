# Development and main delivery

`dev2027` is the default development branch. Local development and VS Code use
this branch. Development pushes and pull requests receive bounded static checks.
Full regression runs on `dev2027` in the daily cycle where this repository has a
regression suite. An unchanged successfully qualified revision is reused.

`main` is the release source. Promote changes through a pull request to `main`.
Main qualification must succeed before application delivery. Manual qualification
and recovery remain available. CI never creates version-bump commits in response
to ordinary development pushes.

Delivery depends on the component:

- Core: qualify the complete main revision, then publish that exact revision to
  Global/RU Root. Their managed node updater prepares slots, checks readiness and
  coordinates hub/member rollout.
- Backend: qualify main contracts, request one immutable image build in the
  infrastructure repository, and deliver it to RU then Global Root through the
  installed storage-aware controller.
- Web client: qualify main contracts and publish Firebase Hosting automatically.
- Native clients and sidecar: build main release packages; signed update admission
  and device installation keep their existing product-specific controls.
- Infrastructure: validate policy and build/deliver the affected backend graph.
  Terraform/DNS changes keep their existing explicit admission; a source push is
  not an arbitrary infrastructure mutation authorization.
- Documentation, registry and supporting libraries: use the same branches and
  repository-specific validation/publication, with no unrelated service rollout.

GitHub branch protection is enabled where the account plan supports it. Private
repositories on the current GitHub Free organization cannot enforce protected
branches; main-only workflow gates and the independent Root admission still
validate the actual producer/source before deployment.

Root CI uses a dedicated forced-command SSH identity. It has no shell, forwarding,
provider keys or Root service token. Incoming data cannot choose a controller,
storage profile, command, arbitrary destination or mutable image tag. The Root
checks GitHub producer metadata and the immutable build receipt before using its
existing controller. Unresolved writer transactions stay recorded for explicit
reconciliation rather than being blindly repeated.
