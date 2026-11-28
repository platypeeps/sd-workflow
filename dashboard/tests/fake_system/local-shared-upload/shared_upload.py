"""A fake of the system checkout's `shared_upload.py` for the dashboard's tests (sd:6044).

What `upload_link` calls: the lifetime and size parsers, the public URL, a presigned POST's fields (a hash stands in
for the Signature Version 4 signature), the curl line, and `credentials`, which no test reaches: each passes its own.
"""

import base64
import datetime
import hashlib
import json
import re
import shlex


def seconds(text):
    found = re.fullmatch(r"([1-9][0-9]*)([mhd])", text)
    if not found:
        raise ValueError(f"--expires takes a count and a unit, m, h or d, such as 168h, not {text!r}")
    return int(found[1]) * {"m": 60, "h": 3600, "d": 86400}[found[2]]


def megabytes(text):
    if not re.fullmatch(r"[1-9][0-9]*", text) or int(text) > 5 * 1024:
        raise ValueError(f"--max-mb takes a whole number of MiB from 1 to 5120, not {text!r}")
    return int(text)


def public_url(bucket, region, key):
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def credentials(profile):
    raise RuntimeError(f"the fake shared_upload reads no keys for profile {profile}")


def presigned_post(bucket, region, prefix, keys, lifetime, max_bytes, now):
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    fields = {"key": prefix + "${filename}", "x-amz-credential": f"{keys['AccessKeyId']}/{stamp[:8]}/{region}",
              "x-amz-date": stamp}
    if keys.get("SessionToken"):
        fields["x-amz-security-token"] = keys["SessionToken"]
    expiry = (now + datetime.timedelta(seconds=lifetime)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conditions = [{"bucket": bucket}, ["starts-with", "$key", prefix], ["content-length-range", 1, max_bytes]]
    fields["policy"] = base64.b64encode(json.dumps({"expiration": expiry, "conditions": conditions}).encode()).decode()
    fields["x-amz-signature"] = hashlib.sha256((keys["SecretAccessKey"] + fields["policy"]).encode()).hexdigest()
    return public_url(bucket, region, ""), fields, expiry


def curl_line(url, fields):
    return " ".join(["curl"] + [f"--form-string {shlex.quote(f'{name}={value}')}" for name, value in fields.items()]
                    + ["-F file=@/path/to/file", url])
