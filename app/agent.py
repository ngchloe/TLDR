# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Optional

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.load_artifacts_tool import LoadArtifactsTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types


async def auto_save_uploaded_files(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> Optional[LlmResponse]:
    """Before every model call, scan user message parts for inline file data.

    When the ADK playground (or any client) passes an uploaded file as inline
    data in the conversation, this callback intercepts it and saves it to the
    artifact service so that validate_files and LoadArtifactsTool can find it.
    A session-state key tracks already-saved files to avoid duplicates.
    """
    saved_keys: list = callback_context.state.get("_saved_upload_keys", [])
    saved_set = set(saved_keys)

    for content in (llm_request.contents or []):
        if content.role != "user":
            continue
        for part in (content.parts or []):
            inline = getattr(part, "inline_data", None)
            if not inline or not getattr(inline, "data", None):
                continue

            mime: str = inline.mime_type or "application/octet-stream"
            data: bytes = inline.data

            # Build a lightweight unique key from size + first 16 bytes
            prefix = data[:16].hex() if isinstance(data, bytes) else str(data)[:16]
            key = f"{len(data)}_{prefix}"

            if key in saved_set:
                continue  # Already saved this file

            # Choose a human-readable filename based on MIME type
            if mime == "application/pdf":
                filename = "uploaded_document.pdf"
            elif mime.startswith("text/"):
                ext = mime.split("/")[-1]
                filename = f"uploaded_document.{ext}"
            elif "wordprocessingml" in mime or "document" in mime:
                filename = "uploaded_document.docx"
            else:
                ext = mime.split("/")[-1]
                filename = f"uploaded_file.{ext}"

            artifact = types.Part.from_bytes(data=data, mime_type=mime)
            await callback_context.save_artifact(filename=filename, artifact=artifact)
            saved_set.add(key)

    # Persist the updated set back to session state
    callback_context.state["_saved_upload_keys"] = list(saved_set)
    return None  # Continue normally to the model


async def validate_files(tool_context: ToolContext) -> str:
    """Lists and validates all uploaded files in this session.

    Checks whether any files have been uploaded, verifies that they are
    valid documents (PDFs, text files, Word docs) and NOT images.

    Args:
        tool_context: Injected context for accessing the artifact service.

    Returns:
        A plain-text validation report describing every found file and its
        validity status.
    """
    try:
        files = await tool_context.list_artifacts()

        # Filter out internal state keys (start with _)
        doc_files = [f for f in files if not f.startswith("_")]

        if not doc_files:
            return (
                "No files have been uploaded to this session yet. "
                "Please upload a PDF or document file using the attachment button."
            )

        results = []
        valid_count = 0

        for filename in doc_files:
            artifact = await tool_context.load_artifact(filename=filename)
            if not artifact or not artifact.inline_data:
                results.append(f"- '{filename}': Could not load — skipping.")
                continue

            mime_type: str = artifact.inline_data.mime_type or ""

            if mime_type.startswith("image/"):
                results.append(
                    f"- '{filename}' ({mime_type}): INVALID ✗ — "
                    "images cannot be summarised. Please upload a PDF or text document."
                )
            elif (
                mime_type == "application/pdf"
                or mime_type.startswith("text/")
                or "document" in mime_type
                or "wordprocessingml" in mime_type
            ):
                results.append(f"- '{filename}' ({mime_type}): VALID ✓")
                valid_count += 1
            else:
                results.append(
                    f"- '{filename}' ({mime_type}): INVALID ✗ — "
                    "only PDF and text documents are supported."
                )

        summary = (
            f"Found {len(doc_files)} file(s); {valid_count} valid document(s).\n"
        )
        return summary + "\n".join(results)

    except ValueError as e:
        return f"Artifact service unavailable: {e}"
    except Exception as e:
        return f"Unexpected error during file validation: {e}"


root_agent = Agent(
    name="tldr_agent",
    model=Gemini(
        model="gemini-2.5-flash",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="""You are TLDR — a helpful PDF and document summarisation assistant.
Follow these three steps strictly, in order:

STEP 1 — VALIDATE THE FILE
• Call the `validate_files` tool every time the user sends a message (the user may upload a file at any turn).
• If no valid file is found, politely ask the user to upload a PDF or text document using the attachment/paperclip button.
• If the file is an image or unsupported type, explain why it is invalid and ask for a document instead.
• Do NOT move to Step 2 until at least one valid document is confirmed.

STEP 2 — ASK FOR THE TARGET AUDIENCE
• Once a valid document is confirmed, ask: "Who is the target audience for this summary?"
• Give 4–5 example audiences (e.g. primary school children, business executives, software engineers, general public, medical professionals).
• Wait for the user's reply. Do NOT generate a summary yet.

STEP 3 — SUMMARISE
• Call the `load_artifacts` tool to load the document content into context.
• Generate a summary tailored precisely to the specified audience:
  - Adjust vocabulary, depth, tone, and examples to suit that audience.
  - Use bullet points or headings if appropriate for the audience.
  - Be concise but complete.

Always return to Step 1 if the user uploads a new file.""",
    tools=[validate_files, LoadArtifactsTool()],
    before_model_callback=auto_save_uploaded_files,
)

app = App(
    root_agent=root_agent,
    name="app",
)
