import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials


basic = HTTPBasic(auto_error=False)


def require_team(password: str):
    def dependency(
        credentials: HTTPBasicCredentials | None = Depends(basic),
    ) -> str:
        valid = credentials is not None
        valid = valid and secrets.compare_digest(credentials.username, "team")
        valid = valid and secrets.compare_digest(credentials.password, password)
        if not valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Credenziali non valide",
                headers={"WWW-Authenticate": "Basic"},
            )
        return "team"

    return dependency
