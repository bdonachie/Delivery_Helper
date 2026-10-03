"""Write an email as a .eml draft file.

The desktop build handed the HTML to Outlook over COM and saved a .msg. A Linux container
has no Outlook, and on a server the file has to travel to the user over HTTP anyway, so
drafts are written as .eml instead: a plain RFC 5322 file with no Windows dependency.

Downloading one and opening it gives the same result as before - Outlook (and Apple Mail,
and Thunderbird) opens a .eml carrying the `X-Unsent: 1` header as an editable draft with
the recipients, subject, body, inline images and attachments already in place, ready to
review and send. Nothing here sends anything.
"""

import mimetypes
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from typing import Iterable, Optional, Tuple

# Tells Outlook to open the file as an unsent draft rather than as a received message.
UNSENT_DRAFT_HEADER = 'X-Unsent'

# Shown by clients that cannot render HTML at all. The real content is always the HTML part.
PLAIN_TEXT_FALLBACK = ('This message is formatted in HTML. '
                       'Open it in an email client that can display HTML.')


def _guess_content_type(file_path: Path) -> Tuple[str, str]:
    guessed_type, _encoding = mimetypes.guess_type(file_path.name)
    if not guessed_type or '/' not in guessed_type:
        return 'application', 'octet-stream'
    main_type, _, sub_type = guessed_type.partition('/')
    return main_type, sub_type


def write_draft(
    output_path: Path,
    subject: str,
    html_body: str,
    recipients: str = '',
    attachments: Optional[Iterable[Tuple[Path, str]]] = None,
    inline_images: Optional[Iterable[Tuple[str, Path]]] = None,
) -> Path:
    """Write one draft and return where it landed.

    `attachments` are (source path, name to show in the email) pairs; a source that is not
    on disk is skipped rather than failing the whole draft, so a missing guide costs an
    attachment and not the send-out. `inline_images` are (content id, source path) pairs
    referenced from the HTML as `cid:<content id>`; they render in the body instead of
    hanging off the message as loose attachments.
    """
    message = EmailMessage()
    message[UNSENT_DRAFT_HEADER] = '1'
    message['Subject'] = subject
    if recipients:
        message['To'] = recipients

    message.set_content(PLAIN_TEXT_FALLBACK)
    message.add_alternative(html_body, subtype='html')

    # add_related has to go on the HTML part itself, so that part becomes the
    # multipart/related container holding the images the HTML refers to.
    html_part = message.get_payload()[-1]
    for content_id, image_path in inline_images or []:
        image_path = Path(image_path)
        if not image_path.is_file():
            continue
        main_type, sub_type = _guess_content_type(image_path)
        html_part.add_related(image_path.read_bytes(), maintype=main_type, subtype=sub_type,
                              cid=f'<{content_id}>', filename=image_path.name)

    for source_path, display_name in attachments or []:
        source_path = Path(source_path)
        if not source_path.is_file():
            continue
        main_type, sub_type = _guess_content_type(source_path)
        message.add_attachment(source_path.read_bytes(), maintype=main_type,
                               subtype=sub_type, filename=display_name)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # SMTP policy gives CRLF line endings, which is what mail clients expect in a .eml.
    output_path.write_bytes(message.as_bytes(policy=SMTP))
    return output_path

