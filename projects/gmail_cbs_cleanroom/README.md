# Gmail CBS Cleanroom

Gmail CBS Cleanroom is an AdaOS beta Application for exercising the
`capability:mail.messages.manage@1.0.0` contract through the governed Google
Gmail provider. AdaOS Core owns OAuth, tokens, refresh, and the fixed
`gmail.googleapis.com` transport. The Application never receives credentials.

## Reusable connection release boundary

The beta candidate retains the existing `gmail_cbs_cleanroom_skill` provider
and Project version `0.1.9`. Forge owns release metadata; the scenario, skill,
and Project version numbers are independent and remain unchanged.
This is the existing provider, with no additional implementation or account store.

Consumers explicitly call the exported `reusable_connections` tool to discover
redacted connection references, then `attach_reusable_connection` with the chosen
`account_id` (`google.gmail`). Discovery does not attach automatically. Attachment
delegates to Core and returns redacted state and an attachment reference, never
credentials. It does not start OAuth or write mailbox data. Core owns credentials,
caller identity, consent and the application-scoped attachment decision.

Both tools require `providers.google.gmail`; discovery additionally requires
`workspace.read`, and attachment requires `workspace.write`. No application roles
or local grants store are introduced. The existing Gmail UI and client operations
are preserved. Reuse tools are provider exports outside the eight-operation
`mail.messages.manage@1.0.0` semantic mapping, preserving its canonical digest.

Trial evidence checkpoints are in the provider package:

- `tests/test_application_contract.py`: trusted context and permission denials,
  including reuse, before provider IO; declaration and absence of a role store.
- `tests/test_behavior_contract.py`: explicit discovery/attachment, empty results,
  invalid references, bounded provider failures and no implicit OAuth/mail writes.
- `tests/test_gmail.py`: existing Gmail behavior and reuse output redaction.
- `tests/test_portable_contract.py`: canonical contract projection/digest and
  hermetic portable adapter conformance.
- The scenario's `tests/test_bindings.py` checks established UI bindings.

These tests use SDK doubles and synthetic records with network calls forbidden.
They do not qualify actual Core authorization, migration checksums, deployed
browser journeys or live Gmail. The trusted worker must seal both Trial checkpoint
artifacts and run native CBS compilation/admission, install-strict validation and
browser checks before Trial delivery. No publication or installation occurs in
this local realization.

## Exact beta checkpoint handoff

`contracts/provider.cbs.yaml` in the owned skill is the sole authored CBS
provider source. The package compiler owns canonical CapabilityContract and
BindingDefinition outputs; `tests/canonical_mail_contract.json` is only the
hermetic conformance oracle. Keep the `mail.messages.manage@1.0.0` semantic
digest unchanged. Presentation tools remain outside its eight-operation mapping.

Run the bounded local checks using the admitted interpreter and inherited SDK
path from the isolated checkout:

```powershell
& $env:ADAOS_PYTHON -m pytest -q skills/gmail_cbs_cleanroom_skill/tests scenarios/gmail_cbs_cleanroom/tests
```

The scenario suite checks exported UI tool signatures, selected-message mutation
wiring, send command identity, Project permissions and the sole authored provider
source. It belongs to the Application closure, not the isolated skill package.
The skill suite checks behavior, access delegation, persistence and canonical
semantic compatibility using hermetic doubles.

The trusted worker must bind these results to the exact candidate source and
seal the separate access and behavior artifacts, run native CBS compilation and
admission and release/install-strict checks, and create the exact Automation
checkpoint for Application Trial. A local test pass is not that checkpoint or
Trial acceptance. Browser checks, deployed authorization, registry publication,
installation and activation remain governed worker operations. Publication must
use the exact stable release digest produced by that lifecycle, never the prior
published release or an invented candidate digest.

## Connect a Gmail account

1. In the Google Cloud project enable **Gmail API**
   (`gmail.googleapis.com`). If it was enabled just now, allow a few minutes for
   propagation.
2. In Google Auth Platform configure **Branding** and **Audience**. If the app
   is External and in Testing, add your Gmail address in **Audience → Test
   users**. In Testing, Google refresh tokens normally expire after seven days.
3. In **Data Access** add the restricted scope:

   ```text
   https://www.googleapis.com/auth/gmail.modify
   ```

   Personal/dev testing can continue through Google's unverified-app warning.
   Public production use requires the applicable Google verification.
4. Create an OAuth Client with type **Web application**. A Desktop application
   client is not suitable for this callback flow.
5. Add this exact Authorized redirect URI (no trailing slash):

   ```text
   http://127.0.0.1:8777/api/providers/google/gmail/oauth/callback
   ```

6. Copy the client ID and secret into AdaOS **Applications → Settings → Google
   OAuth client ID / Google OAuth client secret**, then save. AdaOS stores them
   in the local credential vault. Never place the secret in chat, source,
   fixtures, logs, or telemetry.
7. Return to Gmail CBS Cleanroom, select **Connect Gmail**, choose the test
   account, and approve access. When the browser shows `Gmail connected`, close
   that page and return to AdaOS.

The Application requests `gmail.modify`, which permits reading and organizing
mail and is classified by Google as a restricted scope. Mail content is kept
transient; AdaOS stores only redacted connection state and content-free command
identities needed for safety.
