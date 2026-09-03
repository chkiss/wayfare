# Steps that need root

Everything else in this project runs unprivileged. These four things do not,
so they are written out here to be read first and run deliberately.

## 1. System packages

`tesseract-ocr` is what reads screenshots; `zbar-tools` is what decodes
boarding-pass barcodes. Without them the tool still works on text and on PDFs
that carry a text layer, but it cannot read an image at all.

```sh
sudo apt update
sudo apt install -y tesseract-ocr zbar-tools poppler-utils
```

Add a language pack for any non-English booking you expect
(`tesseract-ocr-fra`, `tesseract-ocr-nld`, `tesseract-ocr-deu`, …).

Optional, and worth it if you often get original booking PDFs rather than
screenshots — KDE's extraction engine, which has hand-written parsers for real
airline and rail documents:

```sh
sudo apt install -y kitinerary          # provides kitinerary-extractor
```

If the binary is not packaged on your distribution, skip it. The tool detects
its absence and carries on.

## 2. The nginx vhost

The config ships in its bootstrap form: port 80 only. Certbot adds the TLS
block in step 3. Adding `listen 443 ssl` before the certificate exists makes
`nginx -t` fail, which blocks reloads for every other site on the box too.

```sh
sudo cp deploy/wayfare-limit.conf /etc/nginx/conf.d/wayfare-limit.conf
sudo cp deploy/nginx-wayfare.conf /etc/nginx/sites-available/wayfare.example.com.conf
sudo ln -s /etc/nginx/sites-available/wayfare.example.com.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

The hostname must already resolve to this server before the next step.

## 3. TLS

```sh
sudo certbot --nginx -d wayfare.example.com
```

Do this **before** putting the owner token into a browser. The token is sent
in a cookie, and over plain HTTP it is sent in clear text to anyone on the
path.

## 4. Firewall

Ports 80 and 443 only. The application itself listens on `127.0.0.1:8791` and
must never be exposed directly:

```sh
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

## Not root

The service runs as your own user, under `systemd --user`:

```sh
mkdir -p ~/.config/systemd/user
cp deploy/wayfare.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now wayfare
```

That survives reboot provided lingering is on for the account
(`loginctl enable-linger <user>`, which does need root, once).
