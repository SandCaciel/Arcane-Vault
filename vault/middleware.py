"""Middleware enforcing Admin Account Approval Gate for authenticated users."""
from django.shortcuts import redirect


EXEMPT_PREFIXES = (
    "/auth/",
    "/api/",
    "/static/",
    "/u/",
    "/explorer",
    "/admin/",
)


class ApprovalGateMiddleware:
    """
    Redirects authenticated users whose approval_status is PENDING or REJECTED
    to the Account Creation Request status screen when accessing private Vault/Deck pages.
    Public profiles (/u/<username>/), Card Explorer (/explorer/), and API endpoints (/api/) remain accessible.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        path = request.path or "/"
        if (
            user
            and user.is_authenticated
            and not user.is_approved
            and not any(path.startswith(prefix) for prefix in EXEMPT_PREFIXES)
        ):
            return redirect("request_access")
        return self.get_response(request)
