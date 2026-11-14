from fastapi import APIRouter

from .. import __version__

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


# HEAD too, so simple checks like `curl -I` work. Kept out of the API description.
router.add_api_route("/health", health, methods=["HEAD"], include_in_schema=False)
