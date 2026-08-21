# Voice Live interim-response test

A deliberately small experiment for testing whether Azure Voice Live remains responsive while a Microsoft Agent Framework tool spends 15 seconds waiting.

## Architecture

```text
Browser (microphone + timeline)
  │  public WSS, PCM16 + JSON
  ▼
Azure Container Apps web/proxy
  │  managed-identity authenticated WSS
  ▼
Foundry Hosted Agent (`invocations_ws`)
  ├─ Azure Voice Live: gpt-4.1-mini + en-US-AndrewMultilingualNeural
  └─ Microsoft Agent Framework: model-router + one 15-second slow_operation tool
```

The browser cannot attach an Entra authorization header to a WebSocket upgrade, so the public web app is a thin authenticated proxy. The hosted agent keeps its audio pumps running while the Agent Framework task executes in a separate asyncio task.

## What the page proves

The page records:

- user and assistant transcripts;
- tool start, per-second waiting, and completion events;
- assistant output that arrives after `tool_started` and before `tool_completed`;
- microphone speech start/stop events while the tool is pending.

Choose either LLM-generated or static interim responses before starting a session. Both use Voice Live's real `tool` trigger.

## Local development

Prerequisites:

- Python 3.13;
- Azure CLI authenticated to the subscription containing the Foundry project;
- the `Foundry User` role on `4iq-foundry-project-resource`.

Create a virtual environment using an approved package source, then install `requirements-dev.txt`. Public `files.pythonhosted.org` access may be blocked by organizational policy.

```bash
cp src/agent/.env.example src/agent/.env
python src/agent/main.py
```

In another terminal, point the proxy at the local hosted-agent route:

```bash
export FOUNDRY_AGENT_WS_ENDPOINT=ws://localhost:8088/invocations_ws
uvicorn app.main:app --app-dir src/web --host 0.0.0.0 --port 8080
```

Open <http://localhost:8080>, choose an interim mode, start the session, and say "Run the slow tool test." Continue speaking during the 15-second wait.

## Deployment

The hosted agent reuses this existing project and deployment:

| Setting | Value |
| --- | --- |
| Project endpoint | `https://4iq-foundry-project-resource.services.ai.azure.com/api/projects/4iq-foundry-project` |
| Project ARM ID | `/subscriptions/27b0139a-16b4-42bf-9ec9-c6db3768245e/resourceGroups/rg-aycabas-3iqs/providers/Microsoft.CognitiveServices/accounts/4iq-foundry-project-resource/projects/4iq-foundry-project` |
| Agent Framework model deployment | `model-router` |
| Voice Live model deployment | `gpt-4.1-mini` |
| Voice | `en-US-AndrewMultilingualNeural` |
| Resource group | `rg-aycabas-3iqs` |

Deployed test:

- Web page: <https://voice-live-test-web.proudbush-5be56d5e.eastus2.azurecontainerapps.io>
- Hosted agent: `voice-live-test`, version 11
- [Foundry playground](https://ai.azure.com/nextgen/r/J7ATmha0Qr-eycbbN2gkXg,rg-aycabas-3iqs,,4iq-foundry-project-resource,4iq-foundry-project/build/agents/voice-live-test/build?version=11)

Deploy the hosted agent with the Foundry `azd ai agent` workflow. Deploy `infra/main.bicep`, build `src/web/Dockerfile` in the provisioned ACR, update the Container App image and `FOUNDRY_AGENT_WS_ENDPOINT`, then grant the web identity `Foundry User` on the existing Foundry account.

No keys or access tokens belong in repository files. Both deployed components use managed identity.

The public URL is protected by Microsoft Entra authentication. Easy Auth requires
sign-in before requests reach the app, and the proxy accepts identities only
from the Microsoft corporate tenant or the BAMI tenant. The Entra app registration
is multi-tenant so both directories can authenticate; the proxy then enforces the
two-tenant allowlist from the authenticated `tid` claim. Its client secret remains
in the Container Apps authentication configuration and is never stored in this
repository.

Voice Live doesn't accept `model-router` as a session model in East US 2, so the
Voice Live conversation uses the existing `gpt-4.1-mini` deployment. The
Microsoft Agent Framework tool path uses the requested `model-router`
deployment.

The deployed WebSocket acceptance test passed in both static and LLM-generated
interim modes. In each run, the client sent another message after `tool_started`,
received assistant text and audio before `tool_completed`, and observed all 15
one-second `tool_waiting` events.

## Acceptance test

1. Open the deployed web page and select **LLM generated**.
2. Start a session and allow microphone access.
3. Ask "What is the status of my simulated operation?" The agent decides to call
   its status tool; there is no manual tool trigger in the UI.
4. Keep talking while the tool card says **Waiting**.
5. Confirm the event timeline contains **OUTPUT DURING TOOL** before **Tool completed**.
6. Repeat with **Static messages** selected.
