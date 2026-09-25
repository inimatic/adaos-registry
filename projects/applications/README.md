# Applications

Applications is the AdaOS control surface for installed, available, and locally developed Applications.

Use it to inspect an Application, open it, pin or unpin it for the current webspace, review permissions and users, complete setup, inspect runtime placement, install or update an exact release, and remove a removable installation.

For local development, the same surface shows Builder Prototype, Automation, Trial, stable publication, CBS lifecycle, and shared-registry publication evidence. `Finalize` promotes an accepted Trial to stable. `Publish` sends the exact stable Application release, its immutable source receipt, and portable CBS semantic artifacts to the configured AdaOS registry. Credentials, local grants, Binding Instances, StateSpaces, and runtime selections are never published.

Registry publication is fail-closed: success requires both semantic publication evidence and an installable public Application catalog entry for the exact release digest.
