"""Errors that map one-to-one onto JSON error responses.

Messages are written for end users: the frontend shows them verbatim.
"""
from __future__ import annotations


class AppError(Exception):
    status_code = 500
    code = "internal_error"
    default_message = "Something went wrong on our side. Please try again."

    def __init__(self, message: str | None = None, *, retry_after: int | None = None) -> None:
        self.message = message or self.default_message
        self.retry_after = retry_after
        super().__init__(self.message)


class InvalidRequest(AppError):
    status_code = 400
    code = "invalid_request"
    default_message = "The request was malformed."


class InvalidURL(AppError):
    status_code = 400
    code = "invalid_url"
    default_message = "That doesn't look like a TikTok video link."


class VideoUnavailable(AppError):
    status_code = 404
    code = "video_unavailable"
    default_message = "This video is private, removed, or doesn't exist."


class FormatUnavailable(AppError):
    status_code = 404
    code = "format_unavailable"
    default_message = "That download isn't available for this video."


class LoginRequired(AppError):
    status_code = 403
    code = "login_required"
    default_message = "TikTok requires a login to view this post (it may be private or age-restricted)."


class TooLarge(AppError):
    status_code = 413
    code = "too_large"
    default_message = "This file is too large for this server to deliver."


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"
    default_message = "Too many requests. Please wait a moment and try again."


class Busy(AppError):
    status_code = 503
    code = "busy"
    default_message = "The server is busy right now. Please try again in a few seconds."


class UpstreamError(AppError):
    status_code = 502
    code = "upstream_error"
    default_message = "TikTok didn't respond as expected. Please try again in a moment."


class UpstreamBlocked(UpstreamError):
    code = "upstream_blocked"
    default_message = (
        "TikTok refused to serve this post to the server. It may be deleted or "
        "region-locked, or TikTok may be blocking this server right now."
    )


class ConversionFailed(AppError):
    status_code = 502
    code = "conversion_failed"
    default_message = "Couldn't convert the audio. Please try again."
