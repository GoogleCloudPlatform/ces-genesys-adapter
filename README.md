# Genesys Cloud to Google Conversational AI Adapter

This repository contains a Python-based adapter that bridges Genesys Cloud AudioHook WebSocket connections with Google's Conversational Agents (Generative) (using the BidiRunSession API). It allows you to integrate your Genesys Cloud contact center with powerful, real-time AI agents.

For a detailed guide on deploying this adapter in a high-availability, multi-region configuration on Google Cloud, please see the [Reference Implementation Guide](docs/reference_implementation/INCUBATION_REFERENCE_IMPLEMENTATION.md).

## Background

### Core Technologies

*   **[Genesys Cloud](https://www.genesys.com/cloud)**: A suite of cloud services for enterprise-grade contact center management. It handles customer communications across voice, chat, email, and other channels.

*   **[AudioHook](https://developer.genesys.cloud/devapps/audiohook/)**: A feature of Genesys Cloud that provides a real-time, bidirectional stream of a call's audio. It uses WebSockets to connect to a service that can monitor, record, or interact with the call audio.

*   **[WebSockets](https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API)**: A communication protocol that enables a two-way interactive communication session between a user's browser or client and a server. It is ideal for real-time applications like live audio streaming.

*   **[Conversational Agents](https://cloud.google.com/customer-engagement-ai/conversational-agents/ps)**: This refers to Google Cloud's powerful platform for building AI-powered conversational experiences. These tools allow you to design, build, and deploy sophisticated voice and chat agents.

### What This Software Does

This application acts as a **bridge** between Genesys Cloud and Google's conversational AI services. It receives the real-time audio stream from a phone call via Genesys AudioHook, forwards that audio to your Google conversational agent for processing, and streams the agent's voice response back into the phone call. This creates a seamless, real-time conversation between the caller and your AI agent.

---

## 1. How to Deploy to Cloud Run (Recommended)

This method deploys the adapter as a scalable, serverless container on Google Cloud Run.

### Step 1: Configure Deployment Values

First, you need to create a configuration file with the specific details for your Google Cloud project and agent.

1.  Copy the example configuration file:
    ```bash
    cp script/values.sh.example script/values.sh
    ```

### Step 1a: Create a Service Account and Configure Authentication

The Cloud Run service needs a Google Cloud service account to run as, which grants it permission to interact with your conversational AI agent.

1.  **Create a service account:** If you don't have one already, create a service account for this adapter.
    ```bash
    gcloud iam service-accounts create [SERVICE_ACCOUNT_NAME] --display-name="Genesys Adapter Service Account"
    ```
    Replace `[SERVICE_ACCOUNT_NAME]` with a name like `genesys-adapter`. The full service account email will be `genesys-adapter@<your-project-id>.iam.gserviceaccount.com`. Use this full email for the `SERVICE_ACCOUNT` variable in your `values.sh` file.

2.  **Choose an authentication method:**

    *   **Option 1 (Recommended): Automatic Authentication**

        Grant the `roles/ces.client` role to your service account. This allows the adapter to automatically generate the necessary credentials to securely connect to your conversational agent.
        ```bash
        gcloud projects add-iam-policy-binding [PROJECT_ID] \
            --member="serviceAccount:[FULL_SERVICE_ACCOUNT_EMAIL]" \
            --role="roles/ces.client"
        ```
        You also need to grant your service account access to the Genesys API key and client secret:
        ```bash
        gcloud secrets add-iam-policy-binding [API_KEY_SECRET_NAME] \
            --member="serviceAccount:[FULL_SERVICE_ACCOUNT_EMAIL]" \
            --role="roles/secretmanager.secretAccessor"

        gcloud secrets add-iam-policy-binding [CLIENT_SECRET_NAME] \
            --member="serviceAccount:[FULL_SERVICE_ACCOUNT_EMAIL]" \
            --role="roles/secretmanager.secretAccessor"
        ```
        Replace `[API_KEY_SECRET_NAME]` and `[CLIENT_SECRET_NAME]` with the names of the secrets you created for the Genesys API key and client secret, respectively.

        With this option, you can leave the `AUTH_TOKEN_SECRET_PATH` variable in `values.sh` empty.

    *   **Option 2 (Advanced): Manual Token Management**

        If your security model requires you to manage access tokens manually, you can specify a path to a secret in Google Secret Manager using the `AUTH_TOKEN_SECRET_PATH` variable in `values.sh`.

        You will need to grant your service account access to the Genesys API key secret, the client secret, and the auth token secret:
        ```bash
        gcloud secrets add-iam-policy-binding [GENESYS_API_KEY_SECRET_NAME] \
            --member="serviceAccount:[FULL_SERVICE_ACCOUNT_EMAIL]" \
            --role="roles/secretmanager.secretAccessor"

        gcloud secrets add-iam-policy-binding [GENESYS_CLIENT_SECRET_NAME] \
            --member="serviceAccount:[FULL_SERVICE_ACCOUNT_EMAIL]" \
            --role="roles/secretmanager.secretAccessor"

        gcloud secrets add-iam-policy-binding [AUTH_TOKEN_SECRET_NAME] \
            --member="serviceAccount:[FULL_SERVICE_ACCOUNT_EMAIL]" \
            --role="roles/secretmanager.secretAccessor"
        ```
        Replace `[GENESYS_API_KEY_SECRET_NAME]`, `[GENESYS_CLIENT_SECRET_NAME]`, and `[AUTH_TOKEN_SECRET_NAME]` with the names of the respective secrets.

        **Important:** You are responsible for ensuring the token in Secret Manager is valid and refreshed periodically. The adapter will simply read and use whatever token is stored there.

### Step 1b: Configure Deployment Values

Open `script/values.sh` in a text editor and fill in the required values. Key variables include:
*   `PROJECT_ID`: Your Google Cloud Project ID.
*   `SERVICE_NAME`: The name you want to give your Cloud Run service (e.g., `genesys-adapter`).
*   `SERVICE_ACCOUNT`: The service account the Cloud Run service will use.
*   `LOCATION`: The Google Cloud region where you want to deploy (e.g., `us-central1`).
*   `GENESYS_API_KEY_SECRET_PATH`: The full resource path to the Secret Manager secret containing the API key that Genesys will use to connect. **Ensure this secret exists and has a value configured.**
*   `GENESYS_CLIENT_SECRET_PATH`: The full resource path to the Secret Manager secret containing the client secret for request signature verification.
*   `LOG_UNREDACTED_DATA`: Set to `true` to log unredacted data from Genesys and CES. Otherwise, sensitive information will be redacted (e.g., `<REDACTED>`). Defaults to `false`.
    **Caution**: This option should typically only be used for local development and debugging purposes. Avoid enabling it in production environments to prevent exposure of sensitive data.

**Note on Agent and Deployment IDs**: You must pass either an agent ID or a deployment ID within the `inputVariables` of the Genesys "open" message.
*   `_agent_id`: The full agent ID.
*   `_deployment_id`: The full deployment ID (e.g., `projects/.../deployments/...`). If you provide a deployment ID, the adapter will automatically extract the agent ID from it and include the deployment ID in the request to the conversational agent.
*   `_initial_message`: (Optional) The initial message text to send to the conversational agent to start the conversation. Defaults to "Hello" if not provided.
*   `_session_id`: (Optional) A custom session ID to use for the conversation. If provided, it will be used as the session ID for the CES connection. If not provided, a random UUID will be generated.

You can set these up in Architect (on the Genesys console) when setting up the integration in your flow. Any other variables in `inputVariables` (not starting with an underscore) will be forwarded to CES.

### Step 2: Run the Deployment Script

Once your `values.sh` file is configured, run the deploy script:

```bash
bash script/deploy.sh
```

This script uses the `gcloud` CLI to build the container image, push it to the Artifact Registry, and deploy it to Cloud Run with all the specified configurations. After deployment, `gcloud` will output the public URL for your service, which you will use to configure the AudioHook in Genesys Cloud.

---

## 2. How to Run Locally for Development

This method is ideal for testing and development. It uses Google Cloud Shell and `ngrok` to expose the local server to the public internet so Genesys Cloud can connect to it.

### Step 1: Open Cloud Shell

Navigate to the [Google Cloud Console](https://console.cloud.google.com) and activate Cloud Shell.

### Step 2: Run the Setup Script

In your first Cloud Shell terminal, run the setup script. This will prepare your environment and start `ngrok`.

```bash
bash script/setup-cloud-shell.sh
```

This script automatically performs the following actions:
*   Installs `ngrok`, a utility to create a secure tunnel to your local environment.
*   Starts `ngrok` and dedicates the terminal to its output.

The script will finish by running `ngrok`, which will display a public "Forwarding" URL (e.g., `https://<random-string>.ngrok-free.app`). This is the secure public URL that you must use for the AudioHook integration in Genesys Cloud.

**Keep this terminal open.**

### Step 3: Run the Server

You will need a second Cloud Shell terminal to run the adapter itself.

1.  **Open a new terminal** and navigate to the project directory.

2.  **Set Environment Variables**: The application requires environment variables to be set. You can create a `.env` file in the root of the project to manage these variables.
    ```bash
    # .env
    PORT=8080
    GENESYS_API_KEY=your_genesys_api_key
    ```

3.  Activate the project's virtual environment:
    ```bash
    . .venv/bin/activate
    ```

4.  Start the adapter application using the development script:
    ```bash
    bash script/run-dev.sh
    ```

### Passing Data from CES to Genesys

At the end of a conversation, your conversational agent can pass data back to Genesys Cloud by using `outputVariables`. The adapter facilitates this by inspecting the `endSession` message from CES.

To send data back to Genesys, your agent should terminate the conversation and include a `params` object within the `metadata` of the `endSession` message. The adapter will automatically convert this `params` object into `outputVariables` that Genesys can use.

**Example `endSession` message from CES:**

If your agent ends the session with the following `endSession` message:
```json
{
  "endSession": {
    "metadata": {
      "params": {
        "disposition": "Resolved",
        "survey_offered": "true",
        "customer_sentiment": "positive"
      }
    }
  }
}
```

**Resulting `outputVariables` in Genesys:**

The adapter will process this and include the following `outputVariables` in the `disconnect` message sent to Genesys:
```json
{
  "outputVariables": {
    "disposition": "Resolved",
    "survey_offered": "true",
    "customer_sentiment": "positive"
  }
}
```

This data can then be used in your Genesys Architect flow for routing decisions, data lookup, or reporting.

---

## Key Features

*   **Barge-in Handling**: Added support for `InterruptionSignal` from CES to handle customer barge-ins, clearing the outbound audio queue.
*   **DTMF Support**: Implemented handling of DTMF messages from Genesys, forwarding digits to CES.
*   **Structured JSON Logging**: Implemented `logging_utils.py` for structured Cloud Logging, enriching logs with session IDs and other context.
*   **Dynamic Initial Message**: Added support for `_initial_message` in input variables, allowing the custom configuration of the conversation kickstart message (defaulting to "Hello").
*   **Custom Session ID**: Added support for `_session_id` in input variables, enabling the caller to provide a custom session ID for the CES conversation.

### Hybrid Secret Management

The adapter supports flexible ways to load secrets like the `GENESYS_CLIENT_SECRET`:

1.  **Cloud Run Secret Injection (Recommended):** You can configure Cloud Run to mount the secret as a file in the container. Provide just the secret name (e.g., `your-genesys-client-secret-name`) in the `GENESYS_CLIENT_SECRET_PATH` environment variable.
2.  **Runtime Fetch from Secret Manager:** You can provide the full secret version resource path (e.g., `projects/<PROJECT_ID>/secrets/<SECRET_ID>/versions/latest`). The adapter will fetch the secret at runtime.

The `src/config.py` module automatically detects the path type and loads the secret accordingly. This is configured via the `GENESYS_CLIENT_SECRET_PATH` variable in `script/values.sh`.

### Enhanced WebSocket Debug Logging

To aid in troubleshooting, the adapter includes detailed WebSocket debug logging:

*   **Enable/Disable:** Controlled by the `DEBUG_WEBSOCKETS` environment variable in `script/values.sh` (set to `true` to enable).
*   **Structured JSON:** Logs are output as JSON, suitable for Cloud Logging.
*   **`websocket_trace` Field:** WebSocket-specific information is nested under the `websocket_trace` key.
*   **Frame Details:** Includes `direction`, `frame_type`, `byte_length`, and `data_preview` for TEXT and BINARY frames.
*   **JSON Content Parsing:** For TEXT frames, the logger attempts to parse the content as JSON.
    *   `data_json`: If parsing is successful, this field contains the parsed object.
    *   `data_json_status`: Indicates the parsing result: `"parsed"`, `"decode_error"`, or `"not_attempted"`.