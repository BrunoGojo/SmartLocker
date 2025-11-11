# app/storage.py
import os
from pathlib import Path
from typing import List, Tuple
import tempfile

STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "azure").lower()

# --- Local backend ---
class LocalStorage:
    def __init__(self, base_dir: str):
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    def save_image(self, user: str, filename: str, content: bytes) -> str:
        user_dir = self.base / user
        user_dir.mkdir(parents=True, exist_ok=True)
        path = user_dir / filename
        path.write_bytes(content)
        return str(path)

    def list_users(self) -> List[str]:
        return [p.name for p in self.base.iterdir() if p.is_dir()]

    def list_user_images(self, user: str) -> List[str]:
        user_dir = self.base / user
        if not user_dir.exists():
            return []
        return [str(p) for p in sorted(user_dir.iterdir()) if p.suffix.lower() in (".jpg", ".jpeg", ".png")]

    def save_model(self, user: str, filename: str, content: bytes) -> str:
        trainer_dir = self.base / user / "trainer"
        trainer_dir.mkdir(parents=True, exist_ok=True)
        path = trainer_dir / filename
        path.write_bytes(content)
        return str(path)

    def list_models(self) -> List[Tuple[str,str]]:
        out = []
        for user_dir in sorted(self.base.iterdir()):
            trainer_dir = user_dir / "trainer"
            if not trainer_dir.exists():
                continue
            for f in trainer_dir.iterdir():
                if f.suffix.lower() == ".yml":
                    out.append((user_dir.name, str(f)))
        return out

    def download_to_temp(self, path: str) -> str:
        # path is local file path already
        return path

    def delete_user(self, user: str) -> bool:
        user_dir = self.base / user
        if not user_dir.exists():
            return False
        import shutil
        shutil.rmtree(user_dir)
        return True

# --- AWS S3 backend ---
class S3Storage:
    def __init__(self, bucket: str, prefix: str = ""):
        import boto3
        self.s3 = boto3.client(
            "s3",
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=os.getenv("AWS_REGION")
        )
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _key(self, *parts):
        parts = [p.strip("/") for p in parts if p]
        key = "/".join([self.prefix] + parts) if self.prefix else "/".join(parts)
        return key

    def save_image(self, user: str, filename: str, content: bytes) -> str:
        key = self._key(user, filename)
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=content)
        return f"s3://{self.bucket}/{key}"

    def list_users(self) -> List[str]:
        paginator = self.s3.get_paginator("list_objects_v2")
        prefix = self.prefix + "/" if self.prefix else ""
        users = set()
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix, Delimiter="/"):
            for common in page.get("CommonPrefixes", []):
                p = common.get("Prefix", "")
                rel = p[len(prefix):].strip("/")
                if rel:
                    users.add(rel)
        if not users:
            resp = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
            for obj in resp.get("Contents", []):
                key = obj["Key"][len(prefix):]
                parts = key.split("/")
                if parts:
                    users.add(parts[0])
        return sorted(list(users))

    def list_user_images(self, user: str) -> List[str]:
        prefix = self._key(user) + "/"
        resp = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        out = []
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if key.endswith((".jpg", ".jpeg", ".png")):
                out.append(f"s3://{self.bucket}/{key}")
        return sorted(out)

    def save_model(self, user: str, filename: str, content: bytes) -> str:
        key = self._key(user, "trainer", filename)
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=content)
        return f"s3://{self.bucket}/{key}"

    def list_models(self) -> List[Tuple[str,str]]:
        prefix = self._key("")  # base prefix
        resp = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        out = []
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".yml"):
                parts = key.split("/")
                if len(parts) >= 3:
                    user = parts[-3] if parts[-2] == "trainer" else parts[-2]
                else:
                    user = parts[-2] if len(parts) >=2 else ""
                out.append((user, f"s3://{self.bucket}/{key}"))
        return out

    def download_to_temp(self, path: str) -> str:
        assert path.startswith("s3://")
        without = path[len("s3://"):]
        bucket, key = without.split("/", 1)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=Path(key).suffix)
        self.s3.download_fileobj(bucket, key, tmp)
        tmp.flush()
        tmp.close()
        return tmp.name

    def delete_user(self, user: str) -> bool:
        prefix = self._key(user) + "/"
        resp = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        keys = [{"Key": obj["Key"]} for obj in resp.get("Contents", [])]
        if not keys:
            return False
        self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})
        return True

# --- Azure Blob backend ---
class AzureBlobStorage:
    def __init__(self, container: str, prefix: str = ""):
        from azure.storage.blob import BlobServiceClient
        conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        if not conn_str:
            raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is required for Azure backend")
        self.client = BlobServiceClient.from_connection_string(conn_str)
        self.container_name = container
        self.container_client = self.client.get_container_client(container)
        try:
            self.container_client.create_container()
        except Exception:
            pass
        self.prefix = prefix.strip("/")

    def _blob_name(self, *parts):
        parts = [p.strip("/") for p in parts if p]
        return "/".join([self.prefix] + parts) if self.prefix else "/".join(parts)

    def save_image(self, user: str, filename: str, content: bytes) -> str:
        blob = self._blob_name(user, filename)
        self.container_client.upload_blob(blob, content, overwrite=True)
        return f"azure://{self.container_name}/{blob}"

    def list_users(self) -> List[str]:
        prefix = self.prefix + "/" if self.prefix else ""
        users = set()
        blobs = self.container_client.list_blobs(name_starts_with=prefix)
        for b in blobs:
            name = b.name[len(prefix):] if prefix else b.name
            parts = name.split("/")
            if parts:
                users.add(parts[0])
        return sorted(list(users))

    def list_user_images(self, user: str) -> List[str]:
        prefix = self._blob_name(user) + "/"
        out = []
        for b in self.container_client.list_blobs(name_starts_with=prefix):
            if b.name.endswith((".jpg", ".jpeg", ".png")):
                out.append(f"azure://{self.container_name}/{b.name}")
        return sorted(out)

    def save_model(self, user: str, filename: str, content: bytes) -> str:
        blob = self._blob_name(user, "trainer", filename)
        self.container_client.upload_blob(blob, content, overwrite=True)
        return f"azure://{self.container_name}/{blob}"

    def list_models(self) -> List[Tuple[str,str]]:
        prefix = self.prefix + "/" if self.prefix else ""
        out = []
        for b in self.container_client.list_blobs(name_starts_with=prefix):
            if b.name.endswith(".yml"):
                parts = b.name.split("/")
                user = parts[-3] if len(parts) >=3 and parts[-2] == "trainer" else parts[-2] if len(parts) >=2 else ""
                out.append((user, f"azure://{self.container_name}/{b.name}"))
        return out

    def download_to_temp(self, path: str) -> str:
        assert path.startswith("azure://")
        without = path[len("azure://"):]
        container, blobname = without.split("/", 1)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=Path(blobname).suffix)
        blob_client = self.client.get_blob_client(container=container, blob=blobname)
        stream = blob_client.download_blob()
        tmp.write(stream.readall())
        tmp.flush()
        tmp.close()
        return tmp.name

    def delete_user(self, user: str) -> bool:
        prefix = self._blob_name(user) + "/"
        blobs = list(self.container_client.list_blobs(name_starts_with=prefix))
        if not blobs:
            return False
        for b in blobs:
            self.container_client.delete_blob(b.name)
        return True

# --- Factory ---
def get_storage():
    if STORAGE_BACKEND == "s3":
        bucket = os.getenv("AWS_S3_BUCKET")
        if not bucket:
            raise RuntimeError("AWS_S3_BUCKET required for s3 backend")
        prefix = os.getenv("STORAGE_PREFIX", "")
        return S3Storage(bucket=bucket, prefix=prefix)
    elif STORAGE_BACKEND == "azure":
        container = os.getenv("AZURE_CONTAINER_NAME")
        if not container:
            raise RuntimeError("AZURE_CONTAINER_NAME required for azure backend")
        prefix = os.getenv("STORAGE_PREFIX", "")
        return AzureBlobStorage(container=container, prefix=prefix)
    else:
        base = os.getenv("DATASET_DIR", str(Path(__file__).resolve().parent.parent / "dataset"))
        return LocalStorage(base)
