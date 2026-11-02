"""Roles and permissions. An admin may do everything, including managing accounts; a user may use the features
an admin has given them."""

ROLES = ("admin", "user")

PERMISSIONS = {  # name -> what it allows, as shown on the admin page
    "check_text": "Paste text",
    "check_documents": "Documents and images (PDF, Word, posters)",
    "check_audio": "Voice notes and audio",
    "check_video": "Videos",
    "ai_review": "AI review with Claude (when the server has a key)",
}

DEFAULT_PERMISSIONS = ("check_text", "check_documents", "check_audio", "check_video")

# What an uploaded file needs, by the kind the file reader detects (app.extract.detect_kind)
KIND_PERMISSION = {
    "pdf": "check_documents",
    "docx": "check_documents",
    "image": "check_documents",
    "text": "check_documents",
    "audio": "check_audio",
    "video": "check_video",
}

SHORT_LABELS = {  # for the activity log
    "check_text": "text",
    "check_documents": "documents",
    "check_audio": "audio",
    "check_video": "video",
    "ai_review": "AI review",
}

KIND_WORDS = {  # for "Your account can't check ..."
    "check_text": "pasted text",
    "check_documents": "documents or images",
    "check_audio": "audio or voice notes",
    "check_video": "videos",
}


def clean_permissions(names) -> frozenset[str]:
    """Known permission names only; raises ValueError for an unknown one."""
    unknown = [name for name in names if name not in PERMISSIONS]
    if unknown:
        raise ValueError(f"Unknown permission: {', '.join(sorted(unknown))}")
    return frozenset(names)
