from app.stores.base import TaskStore
from app.stores.local_store import LocalJsonStore

__all__ = ["TaskStore", "LocalJsonStore", "make_store"]


def make_store(settings) -> TaskStore:
    """依設定選後端：有 Firebase 金鑰用 Firestore（雲端持久化），否則本地 JSON。

    firestore_store 的 firebase_admin 匯入是延遲的，所以本機沒裝 firebase-admin
    也能正常走 JSON 分支。
    """
    if settings.firebase_credentials_file or settings.firebase_credentials_json:
        from app.stores.firestore_store import FirestoreStore

        return FirestoreStore.from_credentials(
            cred_file=settings.firebase_credentials_file,
            cred_json=settings.firebase_credentials_json,
        )
    return LocalJsonStore(settings.data_dir / "output" / "db.json")
