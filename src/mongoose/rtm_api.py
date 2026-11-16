"""HTTP API of mongoose-core: the RTM, unified search, files, email and voicemail routes.

This is the surface the WordPress leaseholder portal plugin and the Cockpit RTM screens talk to.
The assistant app mounts it with app.include_router(router); a core-only deployment can mount
it on its own FastAPI app (see mongoose.core_app)."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from .services.email_index import EmailIn, ingest_email, search_email
from .services.file_index import index_files, search_files
from .settings import get_settings
from .web_common import _require_cockpit_nonce, _require_token_or_cockpit_nonce, _status_cached, _safe_download_name  # noqa: F401

router = APIRouter()


def _rtm_export_dir(body: dict, default_name: str) -> str:
    """Resolve an RTM export dir to the canonical data tree. The cockpit clients send a bare relative
    'data/...' which otherwise resolves against the process cwd (a stray tree that a hygiene sweep
    silently loses). Absolute paths are honoured as-is; a relative one is rooted under data_dir."""
    from pathlib import Path as _P
    raw = str((body or {}).get("dir") or default_name).strip()
    p = _P(raw).expanduser()
    if p.is_absolute():
        return str(p)
    # Strip a leading 'data/' so we don't end up with data_dir/data/...
    parts = p.parts
    if parts and parts[0] == "data":
        p = _P(*parts[1:]) if len(parts) > 1 else _P(default_name)
    return str(get_settings().data_dir / p)


class VoicemailTranscriptIn(BaseModel):
    transcript: str
    source: str = "manual"


@router.post("/cockpit/rtm/documents/upload")
async def rtm_doc_upload(request: Request) -> dict:
    """Ingest an RTM document from the Cockpit: any file + a type (receipt/invoice/contract/ticket/
    feedback/complaint/photo evidence/other evidence). Owner-gated via the cockpit nonce. Stores it,
    text-extracts it, adds it to the owner's unified/global search, and correlates it with the
    voicemails and emails Mongoose already sees. Body: {filename, doc_type, flat_id?, description?, note?,
    content_b64}. A flat_id that is not a real flat is kept as the description, never as a flat."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    import base64
    body = await request.json()
    raw = str(body.get("content_b64") or "")
    try:
        content = base64.b64decode(raw.split(",", 1)[-1]) if raw else b""
    except Exception:
        return {"ok": False, "error": "could not decode the file"}
    from .services.rtm_docs import ingest
    # ingest may run OCR (a Swift subprocess) for image documents, so it must run OFF the event loop.
    return await _asyncio.to_thread(
        ingest, content=content, filename=str(body.get("filename") or "document"),
        doc_type=str(body.get("doc_type") or ""), flat_id=(str(body.get("flat_id")).strip() or None),
        note=str(body.get("note") or ""), description=str(body.get("description") or ""),
        uploaded_by="owner", role="owner", source="cockpit")


@router.get("/cockpit/rtm/documents")
def rtm_doc_list(request: Request, doc_type: str = "", flat_id: str = "", limit: int = 50, offset: int = 0) -> dict:
    """List ingested RTM documents (with correlation counts) plus the subcontext summary. Owner-gated.
    Paginated via limit+offset so the Docs view can load more on demand."""
    _require_cockpit_nonce(request)
    from .services.rtm_docs import list_documents, context_summary
    return {"ok": True,
            "documents": list_documents(doc_type=(doc_type or None), flat_id=(flat_id or None),
                                        limit=limit, offset=offset),
            "context": context_summary()}


@router.get("/cockpit/rtm/documents/{doc_id}")
def rtm_doc_detail(doc_id: int, request: Request) -> dict:
    """One RTM document with its correlated voicemails/emails (the beginnings of Mongoose's meaning)."""
    _require_cockpit_nonce(request)
    from .services.rtm_docs import get_document
    d = get_document(doc_id)
    return {"ok": bool(d), "document": d}


@router.get("/cockpit/rtm/documents/{doc_id}/download")
def rtm_doc_file_download(doc_id: int, request: Request):
    """Download an ingested RTM document's original file (nonce-gated, fetched with the cockpit nonce
    header via phoneDownload). Sensitive leaseholder data, so an unknown id or a missing file is a flat
    404, never a directory hint."""
    _require_cockpit_nonce(request)
    import os as _os
    from fastapi.responses import FileResponse
    from .services.rtm_docs import get_document
    d = get_document(doc_id)
    path = (d or {}).get("stored_path") or ""
    if not d or not path or not _os.path.exists(path):
        raise HTTPException(status_code=404, detail="document file not found")
    fn = _safe_download_name(str(d.get("filename") or f"document-{doc_id}"))
    return FileResponse(path, filename=fn, media_type=str(d.get("mime") or "application/octet-stream"))


@router.get("/cockpit/rtm/documents/{doc_id}/thumb")
def rtm_doc_thumb(doc_id: int, request: Request):
    """Serve the ~480px thumbnail image bytes for a document (owner-gated like the other document
    routes), or 404 if it has none. Produced by the rtm_docs_analyse scheduler job."""
    _require_cockpit_nonce(request)
    from fastapi.responses import FileResponse
    from .services.rtm_docs import get_thumb_path
    path = get_thumb_path(doc_id)
    if not path:
        raise HTTPException(status_code=404, detail="no thumbnail")
    media = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    return FileResponse(path, media_type=media)


@router.post("/cockpit/rtm/pull")
async def rtm_pull_now(request: Request) -> dict:
    """Pull pending RTM documents (and auditor notes) from the WordPress plugin queue and ingest them.
    Mongoose is behind CGNAT so it pulls outbound. No-op with a clear message until WordPress + the
    pull secret are configured. Runs off the event loop (ingest may OCR via a subprocess)."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services.rtm_pull import pull_now
    return await _asyncio.to_thread(pull_now)


@router.post("/cockpit/rtm/documents/{doc_id}/note")
async def rtm_doc_add_note(doc_id: int, request: Request) -> dict:
    """Attach a note to a document without uploading a file (the auditor/read-only seam). Owner-gated
    from the Cockpit today; the same rtm_docs.add_note backs the plugin's auditor path later.
    Body: {text, author?, role?}."""
    _require_cockpit_nonce(request)
    body = await request.json()
    from .services.rtm_docs import add_note
    return add_note(doc_id=doc_id, text=str(body.get("text") or ""),
                    author=str(body.get("author") or "owner"), role=str(body.get("role") or "owner"))


@router.post("/cockpit/rtm/documents/{doc_id}/flat")
async def rtm_doc_set_flat(doc_id: int, request: Request) -> dict:
    """Assign or clear a document's flat, so the backlog of untagged documents can be tagged for the
    per-flat view. A blank/unrecognised value clears it. Owner-gated. Body: {flat_id}."""
    _require_cockpit_nonce(request)
    body = await request.json()
    from .services.rtm_docs import set_document_flat
    return set_document_flat(doc_id, str(body.get("flat_id") or "") or None)


@router.post("/cockpit/rtm/documents/{doc_id}/description")
async def rtm_doc_set_description(doc_id: int, request: Request) -> dict:
    """Set or clear the owner's short description of a document. Owner-gated. Body: {description}."""
    _require_cockpit_nonce(request)
    body = await request.json()
    from .services.rtm_docs import set_document_description
    return set_document_description(doc_id, str(body.get("description") or ""))


@router.post("/cockpit/rtm/documents/{doc_id}/total")
async def rtm_doc_set_total(doc_id: int, request: Request) -> dict:
    """Confirm or correct a document's total by hand; it then overrides anything read from the file.
    A blank total clears the correction. Owner-gated. Body: {total}."""
    _require_cockpit_nonce(request)
    body = await request.json()
    from .services.rtm_docs import set_document_total
    return set_document_total(doc_id, str(body.get("total") or ""))


@router.post("/cockpit/rtm/documents/{doc_id}/delete")
async def rtm_doc_delete(doc_id: int, request: Request) -> dict:
    """Delete ONE uploaded document by its unique id. Removes the row, search-index, correlations, notes and
    governed-evidence for exactly that upload; the stored file is removed only if no duplicate row shares it
    (so deleting one duplicate never removes the original's bytes). Owner-gated from the Cockpit."""
    _require_cockpit_nonce(request)
    from .services.rtm_docs import delete_document
    return delete_document(doc_id)


@router.post("/cockpit/rtm/documents/reprocess")
async def rtm_doc_reprocess(request: Request) -> dict:
    """Re-run extraction (OCR for photos) over documents. Body: {doc_id?} to target one, else backfills
    every document that still has no text. Runs off the event loop (OCR is a Swift subprocess)."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    body = await request.json()
    from .services.rtm_docs import reprocess, reprocess_missing
    did = body.get("doc_id")
    if did is not None:
        return await _asyncio.to_thread(reprocess, int(did))
    return await _asyncio.to_thread(reprocess_missing)


@router.get("/cockpit/rtm/templates")
def rtm_templates_catalogue(request: Request) -> dict:
    """What the unified branded template pack can produce (docx + cover emails), the merge fields each
    needs, and whether the .docx engine is installed. Owner-gated. This is the single source of truth
    the cockpit and (later) the plugin read, so branding/fields cannot drift between surfaces."""
    _require_cockpit_nonce(request)
    from .services import rtm_templates as _T
    return {"ok": True, "catalogue": _T.catalogue()}


@router.post("/cockpit/rtm/templates/generate")
async def rtm_templates_generate(request: Request) -> dict:
    """Generate a branded RTM document from real data. Owner-gated. Body:
      {template, flat_id?, document_number?, recipient_email?, leaseholder_name?, address?, letter_date?}
    - payment_receipt: branded receipt docx for a settled demand (returns a download_url link).
    - cover_email_receipt[/_with_demand]: the receipt cover email; a blank recipient is a DRAFT.
    - welcome (branded_letter): the first-payment welcome letter docx.
    Delegates to rtm_templates.generate_document (the same path chat uses). docx runs off the loop."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    body = await request.json()
    from .services import rtm_templates as _T
    return await _asyncio.to_thread(
        _T.generate_document, str(body.get("template") or "").strip(),
        flat_id=str(body.get("flat_id") or ""), document_number=str(body.get("document_number") or ""),
        recipient_email=str(body.get("recipient_email") or ""),
        leaseholder_name=str(body.get("leaseholder_name") or ""),
        address=str(body.get("address") or ""), letter_date=str(body.get("letter_date") or ""))


@router.post("/cockpit/rtm/export")
async def rtm_export_build(request: Request) -> dict:
    """Build the multiformat accountant export (accounting CSVs, QIF/OFX, XLSX workbook, branded PDF,
    JSON) plus the source-document evidence pack, bundle it into one ZIP, and return a short-lived
    download link. Owner-gated. Body: {formats?:[...], include_evidence?:bool, year?, period?, since?,
    until?}. Heavy (reads the whole ledger, renders PDF/XLSX, zips), so it runs off the event loop."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    body = await request.json()
    from .services import rtm_export as _X
    from .services import rtm_templates as _T
    formats = body.get("formats") or None
    if isinstance(formats, str):
        formats = [formats]
    res = await _asyncio.to_thread(
        _X.build_export,
        year=str(body.get("year") or "2027"),
        period=(str(body["period"]) if body.get("period") else None),
        since=(str(body["since"]) if body.get("since") else None),
        until=(str(body["until"]) if body.get("until") else None),
        formats=formats,
        include_evidence=bool(body.get("include_evidence", True)))
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "export failed")}
    if not res.get("path"):
        return {"ok": False, "error": "pack built but could not be staged for download"}
    token = _T.mint_download(res["path"], res["filename"], ttl_s=3600, media_type="application/zip")
    return {"ok": True, "zip_download_url": f"/rtm/doc/{token}", "filename": res["filename"],
            "size": res.get("size", 0), "manifest": res.get("manifest", {})}


@router.post("/cockpit/rtm/seller-pack/first-stage")
async def rtm_seller_pack_first_stage(request: Request) -> dict:
    """Produce the STAGE-1 LPE1 reply for a flat: the branded cover letter + the statutory Schedule 11
    administration-charge summary, each as a printable PDF download link, plus the plain text and the
    director checklist. Owner-gated. Body: {flat, seller_name?, conveyancer_firm?, conveyancer_contact?,
    conveyancer_email?, their_ref?, request_date?, save?}. ARTIFACTS ONLY: it produces documents, sends
    nothing, and never releases the completed form (that is Stage 2, only after the fee is paid). Renders
    off the event loop (docx + soffice)."""
    _require_cockpit_nonce(request)
    import asyncio as _asyncio
    body = await request.json()
    from .services import rtm_seller_pack_response as _R
    flat = str(body.get("flat") or "").strip()
    if not flat:
        return {"ok": False, "error": "give a flat (id or leaseholder)"}
    job_fields = {k: str(body[k]).strip() for k in ("seller_name", "conveyancer_firm", "conveyancer_contact",
                  "conveyancer_email", "their_ref", "request_date") if body.get(k)}
    if bool(body.get("save")) and job_fields:
        await _asyncio.to_thread(_R.set_job, flat, **job_fields)
    return await _asyncio.to_thread(
        lambda: _R.first_stage_response(flat, download_ttl_s=3600, **job_fields))


@router.get("/rtm/doc/{token}")
def rtm_doc_download(token: str, request: Request):
    """Serve a generated RTM document by its short-lived capability token (minted by generate). The token
    IS the authorisation: it is high-entropy, expires (~1h), and maps to one owner-private file, so a
    plain chat/cockpit link works without a nonce on the browser navigation. Sensitive leaseholder data,
    so an unknown/expired token is a flat 404, never a directory listing."""
    from fastapi.responses import FileResponse
    from .services import rtm_templates as _T
    info = _T.resolve_download(token)
    if not info:
        return JSONResponse({"ok": False, "error": "link expired or not found"}, status_code=404)
    return FileResponse(info["path"], filename=info["filename"], media_type=info["media_type"])


@router.get("/voicemails")
def voicemails_list(request: Request, limit: int = 20, transcript_status: str = "") -> dict:
    """Recent detected voicemails as a separate category from normal email."""
    _require_cockpit_nonce(request)
    from .services.voicemail import list_voicemails, voicemail_stats
    from .services.rtm_example_court import company_profile
    stats = voicemail_stats()
    stats["rtm_tel"] = company_profile().get("tel", "")   # RTM line, from config not a source literal
    return {"ok": True,
            "voicemails": list_voicemails(limit=limit, transcript_status=transcript_status or None),
            "stats": stats}


@router.get("/voicemails/search")
def voicemails_search(request: Request, q: str = Query(..., min_length=1), limit: int = 20) -> dict:
    """Search caller numbers, notice text, audio filenames and voicemail transcripts."""
    _require_cockpit_nonce(request)
    from .services.voicemail import search_voicemails
    return {"ok": True, "query": q, "voicemails": search_voicemails(q, limit=limit)}


@router.get("/voicemail/{voicemail_id}")
def voicemail_detail(voicemail_id: int, request: Request) -> dict:
    _require_cockpit_nonce(request)
    from .services.voicemail import get_voicemail
    vm = get_voicemail(voicemail_id)
    if not vm:
        raise HTTPException(status_code=404, detail="voicemail not found")
    return {"ok": True, "voicemail": vm}


@router.get("/voicemail/{voicemail_id}/audio")
def voicemail_audio_download(voicemail_id: int, request: Request) -> Response:
    _require_cockpit_nonce(request)
    from .services.voicemail import get_voicemail
    from .services.attachments import attachment_payload
    vm = get_voicemail(voicemail_id)
    if not vm:
        raise HTTPException(status_code=404, detail="voicemail not found")
    # Prefer the IMAP-stored attachment bytes; fall back to reading the Pi-side audio over SSH.
    att_id = vm.get("audio_attachment_id")
    if att_id is not None:
        got = attachment_payload(int(att_id))
        if got and got.get("bytes") is not None:
            filename = _safe_download_name(vm.get("audio_filename") or got.get("filename")
                                           or f"voicemail-{voicemail_id}.wav")
            return Response(content=got["bytes"],
                            media_type=got.get("mime_type") or vm.get("audio_mime_type") or "audio/wav",
                            headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    remote_path = (vm.get("remote_audio_path") or "").strip()
    if remote_path:
        import base64 as _base64
        try:
            from .services.protocol_v3 import read_remote_file_b64
        except ImportError:  # core on its own has no Pi transport; fall through to "not available"
            read_remote_file_b64 = None
        got_remote = read_remote_file_b64(remote_path) if read_remote_file_b64 else {"ok": False}
        if got_remote.get("ok"):
            try:
                payload = _base64.b64decode(got_remote["payload_b64"])
            except Exception:
                payload = b""
            if payload:
                filename = _safe_download_name(vm.get("audio_filename") or f"voicemail-{voicemail_id}.wav")
                return Response(content=payload, media_type=vm.get("audio_mime_type") or "audio/wav",
                                headers={"Content-Disposition": f'attachment; filename="{filename}"'})
        raise HTTPException(status_code=404,
                            detail=f"voicemail audio lives on the Pi but could not be read: "
                                   f"{got_remote.get('error') or got_remote.get('detail')}")
    raise HTTPException(status_code=404,
                        detail="voicemail audio bytes unavailable; resync IMAP or connect the Pi staging path")


@router.post("/voicemail/{voicemail_id}/transcript")
def voicemail_transcript_update(voicemail_id: int, payload: VoicemailTranscriptIn,
                                request: Request) -> dict:
    """Paste/edit a transcript. This immediately makes the voicemail transcript searchable."""
    _require_cockpit_nonce(request)
    from .services.voicemail import set_voicemail_transcript
    res = set_voicemail_transcript(voicemail_id, payload.transcript, source=payload.source or "manual")
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("reason") or "voicemail not found")
    return res


@router.get("/find")
def find_everything(q: str = Query(..., min_length=1), limit: int = 10, kinds: str = "") -> dict:
    """Unified search across files (all devices), email attachments, email text, voicemails,
    and notifications."""
    from .services.find import find
    kind_tuple = tuple(k.strip() for k in kinds.split(",") if k.strip()) or None
    return find(q, limit=limit, kinds=kind_tuple)


@router.get("/find/export")
def find_export_route(q: str = Query(..., min_length=1), limit: int = 50,
                      kinds: str = "", fmt: str = "markdown") -> dict:
    """Export a unified search as a portable document (markdown or json). Privacy masking is
    preserved: private items are listed but their snippets stay masked."""
    from .services.find import find_export
    kind_tuple = tuple(k.strip() for k in kinds.split(",") if k.strip()) or None
    return find_export(q, limit=limit, kinds=kind_tuple, fmt=fmt)


@router.get("/rtm/build/source-of-truth")
def rtm_build_source_of_truth_endpoint(request: Request) -> dict:
    """Read-only provenance map: what is canonical for RTM product builds when work can start from
    UI, chat, staged updates, WordPress plugin flows or protocol-v3 peers. Nonce/API-token gated because
    it exposes local paths and build state."""
    _require_token_or_cockpit_nonce(request)
    from .services.rtm_build_source import source_of_truth_status
    return source_of_truth_status()


@router.get("/rtm/portal/readiness")
def rtm_portal_readiness(request: Request) -> dict:
    """Full portal readiness report: bank details, WP connection, keypair, PDFs, 18 flats."""
    _require_token_or_cockpit_nonce(request)
    from .services.rtm_portal import portal_readiness
    return portal_readiness()


@router.get("/rtm/deploy/sync-status")
def rtm_deploy_sync_status(request: Request) -> dict:
    """Live sync verdict between Mongoose and the RTM site: is what the RTM screen shows the same as
    what is deployed and what the live site reports running. Read-only; the verdict is derived on every
    read from the shared deploy-state record, so it can never be stale or disagree with the facts."""
    _require_token_or_cockpit_nonce(request)
    from .services.rtm_deploy_state import sync_status
    st = sync_status()
    # Cheap, no-network signal so the RTM deploy strip can offer the WordPress connect form when
    # Mongoose has no credentials yet (creds-present check only; the live probe stays on Verify).
    try:
        from .settings import get_settings
        _s = get_settings()
        st["wp_configured"] = bool(getattr(_s, "wp_url", "") and getattr(_s, "wp_username", "")
                                   and getattr(_s, "wp_app_password", ""))
    except Exception:
        st["wp_configured"] = False
    return st


@router.post("/rtm/deploy/verify")
async def rtm_deploy_verify(request: Request) -> dict:
    """Actively close the publish/verify loop on demand: GET the RTM site's live build endpoint,
    record what it reports, and return the refreshed sync verdict. Nonce/token-gated. Degrades honestly
    if WordPress is not configured or the site is unreachable (records a heartbeat error, no exception)."""
    _require_token_or_cockpit_nonce(request)
    import asyncio as _aio
    from .services.rtm_deploy_state import verify_live, sync_status
    result = await _aio.to_thread(verify_live)
    return {"verify": result, "sync": sync_status()}


@router.post("/rtm/deploy/deploy-latest")
async def rtm_deploy_latest(request: Request) -> dict:
    """One-tap 'deploy latest': build the current RTM plugin from source, push it to WordPress (which
    records the deployed version + build hash), then immediately verify the live site. Nonce-gated.
    Returns the deploy result, the verify result, and the refreshed sync verdict so the strip updates in
    one round-trip. Requires WordPress creds; without them the deploy step returns a clear reason."""
    _require_cockpit_nonce(request)
    import asyncio as _aio
    from .services.wp_plugin import deploy_plugin_beta
    from .services.rtm_deploy_state import verify_live, sync_status
    deploy = await _aio.to_thread(lambda: deploy_plugin_beta(version_suffix="beta"))
    verify = None
    if isinstance(deploy, dict) and deploy.get("ok"):
        verify = await _aio.to_thread(verify_live)
    # When auto-upload can't work (stock WordPress won't install a custom zip over REST), hand the owner
    # a download link to the built zip so they can upload it once by hand in WP Admin.
    zip_dl = None
    zp = (deploy or {}).get("zip_path") if isinstance(deploy, dict) else None
    if zp:
        try:
            from .services.rtm_templates import mint_download
            from . import profile as _profile
            _tok = mint_download(zp, f"{_profile.wp_plugin_slug()}.zip", ttl_s=3600, media_type="application/zip")
            zip_dl = f"/rtm/doc/{_tok}"
        except Exception:
            zip_dl = None
    return {"deploy": deploy, "verify": verify, "sync": sync_status(), "zip_download_url": zip_dl}


@router.get("/rtm/portal/snapshot")
def rtm_portal_snapshot(request: Request, full: bool = False) -> dict:
    """Build minimised portal snapshot (no excluded fields). full=true for all flats' statements."""
    _require_token_or_cockpit_nonce(request)
    from .services.rtm_portal import build_portal_snapshot
    return build_portal_snapshot(full=full)


@router.post("/rtm/portal/publish")
async def rtm_portal_publish(request: Request) -> dict:
    """Build the minimised snapshot and push it to the WordPress portal sync endpoint."""
    _require_token_or_cockpit_nonce(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    import asyncio as _asyncio
    from .services.rtm_portal import (publish_demand_pdfs, publish_receipt_pdfs, publish_portal_snapshot,
                                       publish_director_summary)
    # OFF the event loop: these make SYNC httpx calls to the remote WordPress site (20-40s timeout each).
    # Inline, a slow/hung example.org would freeze every other request on the single loop.
    full = bool((body or {}).get("full", False))

    def _publish() -> dict:
        snap = publish_portal_snapshot(full=full)
        # Only push the demand PDFs when the snapshot itself landed (which carries their pdf_keys); the
        # portal only shows a Download link once BOTH the snapshot row and the PDF bytes are present.
        pdfs = publish_demand_pdfs() if snap.get("ok") else {"ok": False, "error": "snapshot failed", "pushed": 0}
        # Payment-receipt PDFs ride out the same way (same flat-gated store + download route).
        receipts = publish_receipt_pdfs() if snap.get("ok") else {"ok": False, "error": "snapshot failed", "pushed": 0}
        # Director financials mirror rides out with the same publish (read-only in WP admin).
        director = publish_director_summary() if snap.get("ok") else {"ok": False, "error": "snapshot failed"}
        return {**snap, "demand_pdfs": pdfs, "receipt_pdfs": receipts, "director_summary": director}

    return await _asyncio.to_thread(_publish)


@router.post("/rtm/portal/generate-missing")
async def rtm_portal_generate_missing(request: Request) -> dict:
    """Owner backfill: generate every missing portal document (receipt for a settled demand, PDF for a
    demand with none) and, unless publish=false, publish so they appear on the portal. Same generation
    the WordPress 'generate missing documents' button drives; here it is a one-shot from the Cockpit."""
    _require_token_or_cockpit_nonce(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    import asyncio as _asyncio
    from .services.rtm_portal import (missing_documents, ensure_missing_receipts, regenerate_demand_pdf,
                                       publish_portal_snapshot, publish_demand_pdfs, publish_receipt_pdfs)
    do_publish = bool((body or {}).get("publish", True))

    def _go() -> dict:
        before = missing_documents()
        receipts = ensure_missing_receipts()
        pdfs = [regenerate_demand_pdf(document_number=m["document_number"])
                for m in before.get("missing_demand_pdfs", [])]
        out = {"ok": True, "receipts_generated": receipts.get("count", 0),
               "demand_pdfs_generated": sum(1 for p in pdfs if p.get("ok")),
               "still_missing": missing_documents()}
        if do_publish:
            snap = publish_portal_snapshot(full=True)
            out["published"] = {**snap,
                                "demand_pdfs": publish_demand_pdfs() if snap.get("ok") else None,
                                "receipt_pdfs": publish_receipt_pdfs() if snap.get("ok") else None}
        return out

    return await _asyncio.to_thread(_go)


@router.post("/rtm/portal/announce")
async def rtm_portal_announce(request: Request) -> dict:
    """Email every on-file leaseholder a building announcement (sent by WordPress from the RTM mailbox)
    and record it as a historical announcement in the portal. Owner/Cockpit action."""
    _require_token_or_cockpit_nonce(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    import asyncio as _asyncio
    from .services.rtm_portal import send_portal_announcement
    return await _asyncio.to_thread(send_portal_announcement,
                                    str((body or {}).get("subject", "")), str((body or {}).get("body", "")))


@router.post("/rtm/plan/letter/preview")
async def rtm_plan_letter_preview(request: Request) -> dict:
    """Compose a leaseholder's payment-plan letter and return it WITHOUT sending. The read step that the
    confirm-gated send depends on: the owner sees the exact words before the leaseholder does."""
    _require_token_or_cockpit_nonce(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    import asyncio as _asyncio
    from .services.rtm_portal import build_payment_plan_letter
    return await _asyncio.to_thread(
        build_payment_plan_letter,
        str((body or {}).get("flat", "") or (body or {}).get("flat_id", "")),
        erratum=str((body or {}).get("erratum", "")))


@router.post("/rtm/plan/letter/send")
async def rtm_plan_letter_send(request: Request) -> dict:
    """Email a leaseholder their payment-plan letter. CONFIRM-GATED: the caller must pass confirm=true,
    which is only correct after the preview above has actually been read. Without it this returns the
    draft and sends nothing, because this letter reaching the wrong person with the wrong schedule is the
    specific failure that made the old one-click WordPress send unacceptable."""
    _require_token_or_cockpit_nonce(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    import asyncio as _asyncio
    from .services.rtm_portal import send_payment_plan_letter
    return await _asyncio.to_thread(
        send_payment_plan_letter,
        str((body or {}).get("flat", "") or (body or {}).get("flat_id", "")),
        erratum=str((body or {}).get("erratum", "")),
        confirm=bool((body or {}).get("confirm", False)))


@router.post("/rtm/portal/send-receipt")
async def rtm_portal_send_receipt(request: Request) -> dict:
    """Email a leaseholder their payment receipt (already in the portal store) with a branded cover note,
    sent by WordPress from the RTM mailbox. Owner/Cockpit action."""
    _require_token_or_cockpit_nonce(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    import asyncio as _asyncio
    from .services.rtm_portal import send_receipt_email
    return await _asyncio.to_thread(send_receipt_email,
                                    str((body or {}).get("flat", "") or (body or {}).get("flat_id", "")),
                                    str((body or {}).get("receipt_no", "")))


@router.get("/rtm/director/summary")
async def rtm_director_summary(request: Request) -> dict:
    """Director financials: itemised budget, paid-vs-demanded, svc-charge vs reserves, arrears shortlist."""
    _require_token_or_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services.rtm_director import director_summary
    year = request.query_params.get("year") or None
    return await _asyncio.to_thread(director_summary, year=year)


@router.post("/rtm/director/chase")
async def rtm_director_chase(request: Request) -> dict:
    """Generate (do not send) a chaser draft for one overdue demand."""
    _require_token_or_cockpit_nonce(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    import asyncio as _asyncio
    from .services.rtm_director import chase_draft
    return await _asyncio.to_thread(
        chase_draft,
        document_number=str((body or {}).get("document_number", "")),
        action_type=(body or {}).get("action_type") or None,
    )


@router.get("/rtm/director/members")
async def rtm_director_members(request: Request) -> dict:
    """Full flats/members view: flat, leaseholder name, email, RTM-member flag, last login."""
    _require_token_or_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services.rtm_members import director_members
    return await _asyncio.to_thread(director_members)


@router.post("/rtm/director/member-toggle")
async def rtm_director_member_toggle(request: Request) -> dict:
    """Persist whether a flat's leaseholder is an RTM member (remembered for votes visibility)."""
    _require_token_or_cockpit_nonce(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    import asyncio as _asyncio
    from .services.rtm_members import set_member
    return await _asyncio.to_thread(
        set_member,
        str((body or {}).get("flat_id", "")),
        bool((body or {}).get("is_member", False)),
    )


@router.get("/rtm/votes/list")
async def rtm_votes_list(request: Request) -> dict:
    """All member votes with live tallies (director view). open_only=1 restricts to open votes."""
    _require_token_or_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services.rtm_votes import list_votes
    open_only = request.query_params.get("open_only") in ("1", "true", "yes")
    return await _asyncio.to_thread(list_votes, open_only=open_only)


@router.post("/rtm/votes/create")
async def rtm_votes_create(request: Request) -> dict:
    """Open a new member vote (director only)."""
    _require_token_or_cockpit_nonce(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    import asyncio as _asyncio
    from .services.rtm_votes import create_vote
    opts = (body or {}).get("options") or []
    if isinstance(opts, str):
        opts = [o.strip() for o in opts.split("\n") if o.strip()]
    return await _asyncio.to_thread(
        create_vote,
        question=str((body or {}).get("question", "")),
        options=list(opts),
        closes_at=(body or {}).get("closes_at") or None,
    )


@router.post("/rtm/votes/close")
async def rtm_votes_close(request: Request) -> dict:
    """Close a vote so no further casts are accepted (director only)."""
    _require_token_or_cockpit_nonce(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    import asyncio as _asyncio
    from .services.rtm_votes import close_vote
    return await _asyncio.to_thread(close_vote, int((body or {}).get("vote_id", 0) or 0))


@router.post("/rtm/votes/pull-casts")
async def rtm_votes_pull_casts(request: Request) -> dict:
    """Pull member casts held by WordPress and record them (director only). Mongoose sits behind
    CGNAT so WordPress cannot push; this pull mirrors the claims/issues path."""
    _require_token_or_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services.rtm_portal import pull_vote_casts
    return await _asyncio.to_thread(pull_vote_casts)


@router.post("/rtm/director/pull-accounts")
async def rtm_director_pull_accounts(request: Request) -> dict:
    """Pull per-flat WordPress accounts + last-login AND any director-requested membership changes from
    the portal (director only). Fills the real 'last logged in'/'onboarded' data and applies WP membership
    toggles into the Mongoose ledger; publish to the portal afterwards to confirm member_flat_ids back."""
    _require_token_or_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services.rtm_portal import (pull_flat_accounts, pull_member_changes, pull_payment_marks,
                                      pull_doc_requests)
    def _go():
        accounts = pull_flat_accounts()
        changes = pull_member_changes()
        marks = pull_payment_marks()
        docs = pull_doc_requests()
        return {**accounts, "member_changes": changes, "payment_marks": marks, "doc_requests": docs}
    return await _asyncio.to_thread(_go)


@router.get("/rtm/apportionment/schedules")
async def rtm_appn_schedules(request: Request) -> dict:
    """List schedules as first-class entities."""
    _require_token_or_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services import rtm_apportionment as APP
    def _go():
        APP.seed_default_schedules()
        return {"ok": True, "schedules": APP.list_schedules()}
    return await _asyncio.to_thread(_go)


@router.get("/rtm/apportionment/validate")
async def rtm_appn_validate(request: Request) -> dict:
    """Apportionment integrity across schedules (Σ permille within tolerance of 100%)."""
    _require_token_or_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services import rtm_apportionment as APP
    return await _asyncio.to_thread(APP.validate_all)


@router.post("/rtm/apportionment/set")
async def rtm_appn_set(request: Request) -> dict:
    """Set one flat's ppm/percentage for a schedule (director only)."""
    _require_token_or_cockpit_nonce(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    import asyncio as _asyncio
    from .services import rtm_apportionment as APP
    return await _asyncio.to_thread(
        APP.set_apportionment, flat_id=str((body or {}).get("flat_id", "")),
        schedule=str((body or {}).get("schedule", "")),
        ppm=(body or {}).get("ppm"), pct=(body or {}).get("pct"),
        source_clause=str((body or {}).get("source_clause", "")))


@router.post("/rtm/apportionment/derive")
async def rtm_appn_derive(request: Request) -> dict:
    """Generate per-flat per-schedule demands for a period from the apportionment (director only)."""
    _require_token_or_cockpit_nonce(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    import asyncio as _asyncio
    from .services import rtm_apportionment as APP
    return await _asyncio.to_thread(
        APP.derive_demands, year=str((body or {}).get("year", "")),
        period=str((body or {}).get("period", "")),
        numerator=int((body or {}).get("numerator", 1)),
        denominator=int((body or {}).get("denominator", 2)))


@router.get("/rtm/demand-schedule/status")
async def rtm_demand_schedule_status(request: Request) -> dict:
    """The scheduled next half-year demand run (if any) and its state."""
    _require_token_or_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services import rtm_scheduled_demands as SD
    return await _asyncio.to_thread(SD.status)


@router.post("/rtm/demand-schedule/set")
async def rtm_demand_schedule_set(request: Request) -> dict:
    """Schedule the next half-year demand run (year, period, doc_prefix, prepare_on, issue_date, due_date)."""
    _require_token_or_cockpit_nonce(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    import asyncio as _asyncio
    from .services import rtm_scheduled_demands as SD
    b = body or {}
    return await _asyncio.to_thread(
        SD.set_schedule, year=str(b.get("year", "")), period=str(b.get("period", "")),
        doc_prefix=str(b.get("doc_prefix", "")), prepare_on=str(b.get("prepare_on", "")),
        issue_date=str(b.get("issue_date", "")), due_date=str(b.get("due_date", "")),
        payable_label=str(b.get("payable_label", "")))


@router.post("/rtm/demand-schedule/preview")
async def rtm_demand_schedule_preview(request: Request) -> dict:
    """Dry-run the scheduled run: exact per-flat figures, nothing written."""
    _require_token_or_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services import rtm_scheduled_demands as SD
    return await _asyncio.to_thread(SD.preview)


@router.post("/rtm/demand-schedule/issue")
async def rtm_demand_schedule_issue(request: Request) -> dict:
    """Confirm and ISSUE the scheduled run to the ledger (confirm=true required). Sending is the separate,
    confirm-gated batch mailer."""
    _require_token_or_cockpit_nonce(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    import asyncio as _asyncio
    from .services import rtm_scheduled_demands as SD
    return await _asyncio.to_thread(SD.issue, confirm=bool((body or {}).get("confirm", False)))


@router.post("/rtm/demand-schedule/cancel")
async def rtm_demand_schedule_cancel(request: Request) -> dict:
    """Cancel the scheduled run."""
    _require_token_or_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services import rtm_scheduled_demands as SD
    return await _asyncio.to_thread(SD.cancel_schedule)


@router.get("/rtm/apportionment/reconcile")
async def rtm_appn_reconcile(request: Request) -> dict:
    """Reconciliation invariants for a year/period (apportionment, demand, spend-vs-budget)."""
    _require_token_or_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services import rtm_apportionment as APP
    year = request.query_params.get("year") or ""
    period = request.query_params.get("period") or ""
    total = request.query_params.get("published_total_pence")
    return await _asyncio.to_thread(
        APP.reconcile, year=year, period=period,
        published_period_total_pence=int(total) if total else None)


@router.post("/rtm/apportionment/credit-note")
async def rtm_appn_credit_note(request: Request) -> dict:
    """Issue schedule-level credit notes for an over-recovered schedule (director only). dry_run=1 to
    preview without posting."""
    _require_token_or_cockpit_nonce(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    import asyncio as _asyncio
    from .services import rtm_apportionment as APP
    return await _asyncio.to_thread(
        APP.credit_note_run, schedule=str((body or {}).get("schedule", "")),
        year=str((body or {}).get("year", "")), period=str((body or {}).get("period", "")),
        actual_pence=int((body or {}).get("actual_pence", 0)),
        reason=str((body or {}).get("reason", "")), dry_run=bool((body or {}).get("dry_run", False)))


@router.get("/rtm/portal/review-claims")
async def rtm_portal_review_claims(request: Request) -> dict:
    """Access requests that validated (correct payment ref + demand number) but could not be
    auto-verified by email, awaiting the owner's approve/reject decision."""
    _require_token_or_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services.rtm_portal import list_review_claims
    claims = await _asyncio.to_thread(list_review_claims, "pending")
    return {"ok": True, "claims": claims, "count": len(claims)}


@router.post("/rtm/portal/review-claims/approve")
async def rtm_portal_review_approve(request: Request) -> dict:
    """Owner approves a held access request: provisions the WordPress account + set-password email."""
    _require_token_or_cockpit_nonce(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    import asyncio as _asyncio
    from .services.rtm_portal import approve_review_claim
    return await _asyncio.to_thread(approve_review_claim, int((body or {}).get("claim_id", 0)))


@router.post("/rtm/portal/review-claims/reject")
async def rtm_portal_review_reject(request: Request) -> dict:
    """Owner rejects a held access request: no account is created."""
    _require_token_or_cockpit_nonce(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    import asyncio as _asyncio
    from .services.rtm_portal import reject_review_claim
    return await _asyncio.to_thread(
        reject_review_claim, int((body or {}).get("claim_id", 0)), str((body or {}).get("reason", "")))


@router.post("/rtm/portal/pull-claims")
async def rtm_portal_pull_claims(request: Request) -> dict:
    """Pull encrypted pending claim records from WordPress (metadata + ciphertext only)."""
    _require_token_or_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services.rtm_portal import pull_pending_claims
    return await _asyncio.to_thread(pull_pending_claims)  # sync remote httpx: keep it off the loop


@router.post("/rtm/portal/process-claims")
async def rtm_portal_process_claims(request: Request) -> dict:
    """Decrypt, validate and push decisions for pending claims. Logs counts only, never contents."""
    _require_token_or_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services.rtm_portal import process_pending_claims
    return await _asyncio.to_thread(process_pending_claims)  # sync remote httpx: keep it off the loop


@router.post("/rtm/portal/pull-issues")
async def rtm_portal_pull_issues(request: Request) -> dict:
    """Pull leaseholder issue submissions into RTM intake. Payment issues never touch the ledger."""
    _require_token_or_cockpit_nonce(request)
    import asyncio as _asyncio
    from .services.rtm_portal import pull_portal_issues
    return await _asyncio.to_thread(pull_portal_issues)  # sync remote httpx: keep it off the loop


@router.post("/rtm/portal/attach-demand-pdf")
async def rtm_portal_attach_pdf(request: Request) -> dict:
    """Attach a demand PDF (stored outside the web root; pdf_key + sha256 recorded)."""
    _require_token_or_cockpit_nonce(request)
    body = await request.json()
    from .services.rtm_portal import attach_demand_pdf
    return attach_demand_pdf(document_number=str((body or {}).get("document_number", "")),
                             path=str((body or {}).get("path", "")))


@router.post("/rtm/portal/generate-keypair")
async def rtm_portal_generate_keypair(request: Request) -> dict:
    """Generate the NaCl claim keypair. The private key never leaves this machine."""
    _require_token_or_cockpit_nonce(request)
    from .services.rtm_portal import generate_claim_keypair
    return generate_claim_keypair()


@router.get("/rtm/portal/public-key")
def rtm_portal_public_key(request: Request) -> dict:
    """Return the base64 public key to publish to WordPress for claim encryption."""
    _require_token_or_cockpit_nonce(request)
    from .services.rtm_portal import get_claim_public_key
    return get_claim_public_key()


@router.post("/rtm/drafts/{draft_id}/approve")
def rtm_approve_route(draft_id: str, request: Request) -> dict:
    """Approve an RTM draft reply. Privileged, private, the human-approval gate. The model never
    reaches it. Accepts either the cockpit nonce/master token (Cockpit/direct path) or a
    short-lived draft-scoped one-time token (phone notification tap; the reusable master token
    must never ride in ntfy action headers). With RTM_LIVE_SEND off (default), this approves but
    sends nothing, the send edge only fires on the Mac with the flag on."""
    from .services.rtm_intake import approve_draft, consume_draft_action_token
    draft_token = (request.headers.get("x-rtm-draft-action-token")
                   or request.headers.get("x-draft-action-token") or "").strip()
    if draft_token:
        if not consume_draft_action_token(draft_token, draft_id, "approve"):
            raise HTTPException(status_code=403, detail="invalid, expired, or already-used RTM approval token")
    else:
        _require_token_or_cockpit_nonce(request)
    return approve_draft(draft_id)


@router.post("/rtm/drafts/{draft_id}/cancel")
def rtm_cancel_route(draft_id: str, request: Request) -> dict:
    from .services.rtm_intake import cancel_draft, consume_draft_action_token
    draft_token = (request.headers.get("x-rtm-draft-action-token")
                   or request.headers.get("x-draft-action-token") or "").strip()
    if draft_token:
        if not consume_draft_action_token(draft_token, draft_id, "cancel"):
            raise HTTPException(status_code=403, detail="invalid, expired, or already-used RTM cancel token")
    else:
        _require_token_or_cockpit_nonce(request)
    return cancel_draft(draft_id)


@router.get("/rtm/cases")
def rtm_cases_route() -> dict:
    """Open RTM cases + operational status (always shows whether the live edges are on)."""
    from .services.rtm_intake import list_cases, status
    return {"status": status(), "cases": list_cases()}


@router.get("/rtm/drafts")
def rtm_drafts_route() -> dict:
    from .services.rtm_intake import list_drafts
    return {"pending": list_drafts("pending")}


@router.get("/rtm/secretary/status")
def rtm_secretary_status_route() -> dict:
    from .services import rtm_secretary as S
    return S.status_report()


@router.post("/rtm/secretary/discover")
def rtm_secretary_discover_route(request: Request) -> dict:
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_secretary as S
    return S.compute_compliance()


@router.post("/rtm/secretary/configure")
def rtm_secretary_configure_route(body: dict, request: Request) -> dict:
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_secretary as S
    S.configure_profile(**(body or {}))
    return S.compute_compliance()


@router.get("/rtm/books/summary")
def rtm_books_summary_route() -> dict:
    from .services import rtm_bookkeeper as B
    return B.summary()


@router.post("/rtm/books/transaction")
def rtm_books_transaction_route(body: dict, request: Request) -> dict:
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_bookkeeper as B
    try:
        return B.record_transaction(**(body or {}))
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/rtm/books/balance")
def rtm_books_balance_route(body: dict, request: Request) -> dict:
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_bookkeeper as B
    try:
        return B.record_bank_balance(**(body or {}))
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/rtm/books/reconcile")
def rtm_books_reconcile_route() -> dict:
    from .services import rtm_bookkeeper as B
    return B.reconcile()


@router.get("/rtm/bank-feed/status")
def rtm_bank_feed_status_route() -> dict:
    from .services import rtm_bank_feed_playbook as P
    return P.playbook_status()


@router.get("/rtm/bank-feed/playbook")
def rtm_bank_feed_playbook_route(provider: str = "") -> dict:
    from .services import rtm_bank_feed_playbook as P
    return P.provider_playbook(provider or None)


@router.get("/rtm/bank-feed/messages")
def rtm_bank_feed_messages_route() -> dict:
    from .services import rtm_bank_feed_playbook as P
    return {"templates": P.list_message_templates()}


@router.get("/rtm/bank-feed/messages/{key}")
def rtm_bank_feed_message_route(key: str) -> dict:
    from .services import rtm_bank_feed_playbook as P
    try:
        return P.get_message_template(key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/rtm/bank-feed/accounts")
def rtm_bank_feed_account_route(body: dict, request: Request) -> dict:
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_bank_feed_playbook as P
    try:
        return P.upsert_account(**(body or {}))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/rtm/bank-feed/tests")
def rtm_bank_feed_test_route(body: dict, request: Request) -> dict:
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_bank_feed_playbook as P
    try:
        return P.record_test(**(body or {}))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/rtm/bank-feed/evidence")
def rtm_bank_feed_evidence_route(body: dict, request: Request) -> dict:
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_bank_feed_playbook as P
    try:
        return P.record_evidence(**(body or {}))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/rtm/bank-feed/export-pack")
def rtm_bank_feed_export_route(body: dict, request: Request) -> dict:
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_bank_feed_playbook as P
    return P.export_pack(_rtm_export_dir(body, "rtm_bank_feed_playbook"))


@router.get("/rtm/manage/status")
def rtm_manage_status_route() -> dict:
    from .services import rtm_management as M

    def _compute() -> dict:
        return {"position": M.fund_position(), "arrears": M.arrears(), "cashflow": M.cashflow()}

    # Same SWR treatment as /rtm/manage/dashboard: this recomputes three DB aggregations per call and
    # is polled, so cache it for 30s instead of paying the cost on every request.
    return _status_cached("rtm_manage_status", 30.0, _compute)


@router.get("/rtm/manage/demands")
def rtm_manage_demands_route(status: str = "", flat: str = "", limit: int = 100, period: str = "") -> dict:
    """Demands, optionally narrowed by status/flat/period. `period` is the charging cycle ('2026-H2');
    omit it for every period. Also returns the period list so a caller can render a selector without a
    second round-trip, and can never show periods that disagree with the rows."""
    from .services import rtm_management as M
    return {"demands": M.list_demands(status=status or None, flat=flat or None, limit=limit,
                                      period=period or None),
            "periods": M.list_periods()}


@router.get("/rtm/manage/periods")
def rtm_manage_periods_route() -> dict:
    """Charging periods that exist, newest first, with per-period charged/paid/outstanding and which is
    current. The single source both the Cockpit and the WordPress plugin drive their selector from."""
    from .services import rtm_management as M
    return M.list_periods()


@router.post("/rtm/manage/demands")
def rtm_manage_create_demand_route(body: dict, request: Request) -> dict:
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_management as M
    return M.create_demand(**(body or {}))


@router.post("/rtm/manage/seed-flats")
def rtm_manage_seed_flats_route(request: Request) -> dict:
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_management as M
    return M.seed_flats()


@router.get("/rtm/manage/flats")
def rtm_manage_flats_route() -> dict:
    from .services import rtm_management as M
    flats = M.list_flats()
    # Enrich each flat with its live balance + position (clear / arrears), so the Flats view is a real
    # per-flat MONEY POSITION (who is paid up, who owes) rather than a useless name list. flat_statement
    # is a cheap local computation over the ledger.
    for f in flats:
        try:
            st = M.flat_statement(str(f.get("flat_id") or ""))
            f["balance"] = st.get("balance")
            f["balance_pence"] = st.get("balance_pence")
            f["position"] = st.get("position")
        except Exception:
            pass
    return {"flats": flats}


@router.get("/rtm/manage/statement/{flat_id}")
def rtm_manage_statement_route(flat_id: str) -> dict:
    from .services import rtm_management as M
    return M.flat_statement(flat_id)


@router.get("/rtm/manage/arrears")
def rtm_manage_arrears_route() -> dict:
    from .services import rtm_management as M
    return M.arrears()


@router.post("/rtm/manage/metro-import")
def rtm_manage_metro_import_route(body: dict, request: Request) -> dict:
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_management as M
    try:
        return M.import_metro_csv(str((body or {}).get("path", "")))
    except (FileNotFoundError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/rtm/manage/bank-transactions")
def rtm_manage_bank_transactions_route(request: Request, limit: int = 30) -> dict:
    """Recent imported bank credits with their match/allocation status, so the cockpit can drive
    allocation. Records only; the bank CSV import is the sole way money appears here."""
    _require_token_or_cockpit_nonce(request)
    from .db import connect
    with connect() as con:
        rows = con.execute(
            "SELECT id, bank_date, description, reference, amount_pence, match_status, source, "
            "matched_flat_id, matched_document_number, allocated_pence FROM rtm_mgmt_bank_transactions "
            "ORDER BY bank_date DESC, created_at DESC LIMIT ?", (max(1, min(int(limit), 200)),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["amount"] = f"\u00a3{(d.get('amount_pence') or 0)/100:.2f}"
        d["unallocated_pence"] = int(d.get("amount_pence") or 0) - int(d.get("allocated_pence") or 0)
        out.append(d)
    return {"ok": True, "transactions": out}


@router.post("/rtm/manage/allocate")
def rtm_manage_allocate_route(body: dict, request: Request) -> dict:
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_management as M
    try:
        return M.allocate_payment(str((body or {}).get("bank_transaction_id", "")), list((body or {}).get("allocations", [])))
    except (ValueError, KeyError) as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/rtm/manage/mark-paid")
def rtm_manage_mark_paid_route(body: dict, request: Request) -> dict:
    """Mark a flat's demand as paid (records a received bank credit + allocation; never moves money).
    The one canonical mark-paid the Cockpit, chat and plugin all use. Resolve by flat or document_number;
    amount defaults to the outstanding (a full payment)."""
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_management as M
    try:
        return M.record_payment(
            flat=str((body or {}).get("flat", "")),
            document_number=str((body or {}).get("document_number", "")),
            amount=(body or {}).get("amount"),
            received_date=(body or {}).get("received_date") or None,
            reference=str((body or {}).get("reference", "")),
            method=str((body or {}).get("method", "Bank transfer") or "Bank transfer"))
    except (ValueError, KeyError) as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/rtm/manage/record-on-account")
def rtm_manage_record_on_account_route(body: dict, request: Request) -> dict:
    """Record money received ON ACCOUNT: an advance for a budget not yet demanded, or a payment with no
    open demand. There are no refunds, so it becomes standing credit that the next demand raised consumes.
    (An overpayment of an existing demand does not need this route: /rtm/manage/mark-paid with an amount
    above the outstanding now carries the excess to account automatically.)"""
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_management as M
    try:
        return M.record_on_account_credit(
            flat=str((body or {}).get("flat", "")),
            amount=(body or {}).get("amount"),
            received_date=(body or {}).get("received_date") or None,
            reference=str((body or {}).get("reference", "")),
            method=str((body or {}).get("method", "Bank transfer") or "Bank transfer"),
            note=str((body or {}).get("note", "")))
    except (ValueError, KeyError) as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/rtm/manage/assign-on-account")
def rtm_manage_assign_on_account_route(body: dict, request: Request) -> dict:
    """Pin a flat's standing on-account credit to a fund/schedule (e.g. 'reserve' or 'service') before a
    demand consumes it. Net-zero to the balance; only changes which schedule the credit lands on."""
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_management as M
    try:
        return M.assign_on_account_to_fund(
            flat=str((body or {}).get("flat", "")),
            fund_schedule=str((body or {}).get("fund_schedule", "") or (body or {}).get("fund", "")),
            amount=(body or {}).get("amount"))
    except (ValueError, KeyError) as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/rtm/manage/on-account/{flat}")
def rtm_manage_on_account_status_route(flat: str, request: Request) -> dict:
    """What standing on-account credit a flat holds, and its overall balance/position."""
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_management as M
    st = M.flat_statement(flat)
    return {"ok": True, "flat_id": st.get("flat_id"), "position": st.get("position"),
            "balance": st.get("balance"), "in_credit": st.get("in_credit"),
            "credit_display": st.get("credit_display"),
            "available_on_account": M._money(M.available_on_account_credit(flat))}


@router.get("/rtm/manage/payables")
def rtm_manage_payables_route(status: str = "") -> dict:
    from .services import rtm_management as M
    return {"payables": M.list_payables(status=status or None)}


@router.post("/rtm/manage/payables")
def rtm_manage_add_payable_route(body: dict, request: Request) -> dict:
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_management as M
    return M.add_payable(**(body or {}))


@router.get("/rtm/manage/budget-actuals")
def rtm_manage_budget_actuals_route(year: str = "") -> dict:
    from .services import rtm_management as M
    return M.budget_actuals(year or None)


@router.post("/rtm/manage/evidence")
def rtm_manage_evidence_route(body: dict, request: Request) -> dict:
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_management as M
    return M.add_evidence(**(body or {}))


@router.get("/rtm/manage/dashboard")
def rtm_manage_dashboard_route() -> dict:
    from .services import rtm_management as M
    return _status_cached("rtm_dashboard", 30.0, M.management_dashboard)


@router.post("/rtm/manage/import-flats")
def rtm_manage_import_flats_route(body: dict, request: Request) -> dict:
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_management as M
    return M.import_flats_csv(str((body or {}).get("path", "")))


@router.post("/rtm/manage/import-demands")
def rtm_manage_import_demands_route(body: dict, request: Request) -> dict:
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_management as M
    return M.import_demands_csv(str((body or {}).get("path", "")))


@router.post("/rtm/manage/reconcile-metro")
def rtm_manage_reconcile_metro_route(body: dict, request: Request) -> dict:
    """Safe Metro Bank CSV reconciliation. DRY-RUN by default (apply omitted/false): classifies every
    credit (allocate / confirms-manual / internal-transfer / pre-acquisition / unmatched) and writes
    nothing. apply=true commits: allocates real new payments, records bank-confirmation evidence for
    already-paid demands (never a second allocation), and files the CSV as evidence. Owner-only."""
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_management as M
    b = body or {}
    path = str(b.get("path", "")).strip()
    if not path:
        return {"ok": False, "error": "path to a Metro CSV is required"}
    kwargs = {"apply": bool(b.get("apply", False))}
    if b.get("since"):
        kwargs["since"] = str(b["since"]).strip()
    return M.reconcile_metro_csv(path, **kwargs)


@router.get("/rtm/manage/subaccount-audit")
def rtm_manage_subaccount_audit_route(year: str = "2027") -> dict:
    """Trust-money segregation audit: reserve fund vs service fund, what the payments imply should be in
    each subaccount, versus what was actually moved into each at the bank (from reconciled Metro transfers).
    Read-only."""
    from .services import rtm_management as M
    return M.subaccount_audit(year=year)


@router.post("/rtm/manage/export-pack")
def rtm_manage_export_pack_route(body: dict, request: Request) -> dict:
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_management as M
    return M.export_management_pack(_rtm_export_dir(body, "rtm_management_export"), year=(body or {}).get("year") or None)


@router.post("/rtm/receipt/build")
def rtm_receipt_build_route(body: dict, request: Request) -> dict:
    """Build a receipt for a fully paid demand. Records only; never moves money."""
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_trust as TR
    b = body or {}
    amount = b.get("amount_pence")
    if amount is None and b.get("amount") is not None:
        amount = TR._money_to_pence(b.get("amount"))
    return TR.build_receipt(
        str(b.get("flat", "")), str(b.get("document_number", b.get("document", ""))),
        str(b.get("payment_ref", "")), int(amount or 0),
        str(b.get("received_date", b.get("received", ""))),
        method=str(b.get("method", "Bank transfer")),
    )


@router.post("/rtm/receipt/render")
def rtm_receipt_render_route(body: dict, request: Request) -> dict:
    """Resolve the flat's BRANDED receipt PDF (template-engine pack: receipt page then the original
    demand), building it if not stored yet. Records only; never moves money."""
    _require_token_or_cockpit_nonce(request)
    from .services.rtm_portal import resolve_receipt_pdf
    b = body or {}
    flat = str(b.get("flat", ""))
    document = str(b.get("document_number", b.get("document", "")))
    path = resolve_receipt_pdf(flat, document)
    if not path:
        return {"ok": False, "reason": "receipt_not_found; build it first"}
    return {"ok": True, "path": path}


@router.get("/rtm/receipt/download")
def rtm_receipt_download_route(request: Request, flat: str = "", document: str = "") -> Response:
    """Stream the flat's BRANDED receipt PDF (the same template-engine pack WordPress/email send: the
    receipt page followed by the demand), building it if not stored yet. Records only; never moves money."""
    _require_token_or_cockpit_nonce(request)
    from .services.rtm_portal import resolve_receipt_pdf
    path = resolve_receipt_pdf(flat, document)
    if not path:
        raise HTTPException(status_code=404, detail="receipt not found; record the payment first")
    from fastapi.responses import FileResponse
    fname = f"receipt-{(flat or 'flat').replace('/', '-')}-{document.replace('/', '-')}.pdf"
    return FileResponse(path, media_type="application/pdf", filename=fname)


@router.get("/rtm/reserve/position")
def rtm_reserve_position_route() -> dict:
    """Reserve collected / transferred / in-transit and the reserve trust balance. Records only."""
    from .services import rtm_trust as TR
    return TR.reserve_position()


@router.post("/rtm/reconcile/weekly")
def rtm_reconcile_weekly_route(body: dict, request: Request) -> dict:
    """Weekly reconciliation across the three accounts. Records only; never moves money."""
    _require_token_or_cockpit_nonce(request)
    from .services import rtm_trust as TR
    return TR.reconcile_weekly(as_of=(body or {}).get("as_of") or None)


@router.get("/rtm/transfers/status")
def rtm_transfers_status_route() -> dict:
    """Sweep obligations (in_sync/due/overdue) and the exposure/raid/unfunded/half-open flags. Records only."""
    from .services import rtm_trust as TR
    return TR.transfer_status()


@router.post("/email/ingest")
def email_ingest(item: EmailIn) -> dict:
    return ingest_email(item)


@router.get("/email/search")
def email_search(q: str = Query(..., min_length=1), limit: int = 10) -> dict:
    return {"results": search_email(q, limit)}


@router.get("/email/recent")
def email_recent(request: Request, limit: int = 10, box: str = "inbox") -> dict:
    """Last N emails (newest first) for the RTM mail glance, split by box: 'inbox' = received, 'sent' =
    sent from this mailbox. Returns sender, recipients, subject, snippet, date. Nonce-gated because it
    returns message metadata and a body snippet, same guard as the message-detail view it hands off to."""
    _require_cockpit_nonce(request)
    from .services.email_index import latest_email
    box = "sent" if str(box).lower() == "sent" else "inbox"
    return {"ok": True, "box": box, "results": latest_email(limit, box=box)}


@router.get("/email/message/{message_id}")
def email_message_detail(message_id: int, request: Request) -> dict:
    """Full email detail for click-to-expand search results. Nonce-gated because it returns body text."""
    _require_cockpit_nonce(request)
    from .services.email_index import get_email_message
    msg = get_email_message(message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="email not found")
    return {"ok": True, "message": msg}


@router.get("/email/message/{message_id}/download")
def email_message_download(message_id: int, request: Request, format: str = "txt") -> Response:
    """Download a searched email as txt, eml, or a simple PDF."""
    _require_cockpit_nonce(request)
    from .services.email_index import email_as_bytes
    got = email_as_bytes(message_id, format)
    if not got:
        raise HTTPException(status_code=404, detail="email not found")
    data, media_type, filename = got
    return Response(content=data, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{_safe_download_name(filename)}"'})


@router.get("/email/attachment/{attachment_id}/download")
def email_attachment_download(attachment_id: int, request: Request) -> Response:
    """Download one stored email attachment. Older indexed attachments may only have metadata."""
    _require_cockpit_nonce(request)
    from .services.attachments import attachment_payload
    got = attachment_payload(attachment_id)
    if not got:
        raise HTTPException(status_code=404, detail="attachment not found")
    data = got.get("bytes")
    if data is None:
        raise HTTPException(status_code=404, detail="attachment bytes were not stored for this older indexed item; resync mail to enable attachment download")
    filename = _safe_download_name(got.get("filename") or f"attachment-{attachment_id}")
    media_type = got.get("mime_type") or "application/octet-stream"
    return Response(content=data, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/email/message/{message_id}/attachments.zip")
def email_attachments_zip(message_id: int, request: Request) -> Response:
    """Download all stored attachment bytes for a message as a zip."""
    _require_cockpit_nonce(request)
    import io
    import zipfile
    from .services.email_index import get_email_message
    from .services.attachments import attachment_payload
    msg = get_email_message(message_id, include_body=False)
    if not msg:
        raise HTTPException(status_code=404, detail="email not found")
    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for att in msg.get("attachments", []):
            got = attachment_payload(int(att.get("id"))) if att.get("id") is not None else None
            if not got or got.get("bytes") is None:
                continue
            zf.writestr(_safe_download_name(got.get("filename") or f"attachment-{att.get('id')}"), got["bytes"])
            count += 1
    if count == 0:
        raise HTTPException(status_code=404, detail="no stored attachment bytes available for this message; resync mail to enable attachment download")
    name = _safe_download_name((msg.get("subject") or "email") + " attachments.zip")
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/files/index")
def files_index(root: str, limit: int = 1000) -> dict:
    return index_files(root, limit)


@router.get("/files/search")
def files_search(q: str = Query(..., min_length=1), limit: int = 10) -> dict:
    return {"results": search_files(q, limit)}
