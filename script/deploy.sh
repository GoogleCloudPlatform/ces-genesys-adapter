#!/bin/bash

# Copyright 2025 Google LLC

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     https://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

source $(dirname "$0")/values.sh

for REGION in "${REGIONS[@]}"
do
  echo "Deploying to region: $REGION"

  ENV_VARS_LIST=()
  SECRETS_LIST=()

  if [[ "$GENESYS_API_KEY_SECRET_PATH" == projects/* ]]; then
    ENV_VARS_LIST+=("GENESYS_API_KEY=$GENESYS_API_KEY_SECRET_PATH")
  else
    SECRETS_LIST+=("GENESYS_API_KEY=${GENESYS_API_KEY_SECRET_PATH}:latest")
  fi

  if [[ "$GENESYS_CLIENT_SECRET_PATH" == projects/* ]]; then
    ENV_VARS_LIST+=("GENESYS_CLIENT_SECRET=$GENESYS_CLIENT_SECRET_PATH")
  else
    SECRETS_LIST+=("GENESYS_CLIENT_SECRET=${GENESYS_CLIENT_SECRET_PATH}:latest")
  fi

  # Add other environment variables from values.sh
  ENV_VARS_LIST+=("AUTH_TOKEN_SECRET_PATH=${AUTH_TOKEN_SECRET_PATH}")
  ENV_VARS_LIST+=("NUMBERS_COLLECTION_ID=${NUMBERS_COLLECTION_ID}")
  ENV_VARS_LIST+=("LOG_UNREDACTED_DATA=${LOG_UNREDACTED_DATA}")
  ENV_VARS_LIST+=("DEBUG_WEBSOCKETS=${DEBUG_WEBSOCKETS}")

  GCLOUD_CMD=(gcloud run deploy "$SERVICE_NAME")
  GCLOUD_CMD+=(--source=".")
  GCLOUD_CMD+=(--platform=managed)
  GCLOUD_CMD+=(--region="$REGION")
  GCLOUD_CMD+=(--cpu="$CPU")
  GCLOUD_CMD+=(--memory="$MEMORY")
  GCLOUD_CMD+=(--min-instances="$MIN_INSTANCES")
  GCLOUD_CMD+=(--max-instances="$MAX_INSTANCES")
  GCLOUD_CMD+=(--service-account="$SERVICE_ACCOUNT")
  GCLOUD_CMD+=(--ingress=internal-and-cloud-load-balancing)
  GCLOUD_CMD+=(--allow-unauthenticated)
  GCLOUD_CMD+=(--project="$PROJECT_ID")
  GCLOUD_CMD+=(--timeout="$TIMEOUT")
  GCLOUD_CMD+=(--concurrency="$CONCURRENCY")
  GCLOUD_CMD+=(--startup-probe="httpGet.path=/health,httpGet.port=8080")

  if [ ${#ENV_VARS_LIST[@]} -gt 0 ]; then
    ENV_VARS=$(IFS=,; echo "${ENV_VARS_LIST[*]}")
    GCLOUD_CMD+=(--set-env-vars="$ENV_VARS")
  fi

  if [ ${#SECRETS_LIST[@]} -gt 0 ]; then
    SECRETS=$(IFS=,; echo "${SECRETS_LIST[*]}")
    GCLOUD_CMD+=(--set-secrets="$SECRETS")
  fi

  echo "Running command: ${GCLOUD_CMD[*]}"
  "${GCLOUD_CMD[@]}"

done
