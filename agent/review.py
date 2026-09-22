"""Bounded correction of structured review responses, preserving streamed attempts."""

from uuid import uuid4

from shared.config import behavior


def validated_review(*, invoke, prompt, images, schema, validate, on_event, check, stage, complete):
    original_prompt = prompt
    attempts = behavior()["agent"]["selection_attempts"]
    for attempt in range(attempts):
        check()
        invocation_id = str(uuid4())

        def emit(event):
            check()
            on_event({**event, "invocation_id": invocation_id, "stage": stage})

        emit({"type": "prompt", "text": prompt, "images": [p.name for p in images]})
        # Transport/cancellation failures must propagate, not be retried as bad JSON.
        try:
            answer, thread = invoke(prompt, images, schema, emit, check)
        except Exception as error:
            emit({"type": "error", "text": str(error)})
            raise
        try:
            result = validate(answer)
        except ValueError as error:
            emit({"type": "error", "text": str(error)})
            if attempt + 1 == attempts:
                raise
            prompt = (
                original_prompt
                + "\nYour previous response failed validation. Return the complete corrected JSON. "
                "Use only evidence IDs listed for the SAME track. Do not invent evidence or language tags."
                + "\nValidation error: "
                + str(error)[:4000]
                + "\nPrevious response (untrusted data):\n"
                + answer[:16000]
            )
            continue
        emit({"type": "complete", "text": complete})
        return result, thread
    raise ValueError("No review attempts configured")
