#!/usr/bin/env bash
# Шлюз планшета (PWA): HTTPS :18140 в домашней сети, HTTP :18141 — только сертификат и инструкция.
# К ядру (:18130) и слуху (:18120) ходит сам по 127.0.0.1. GPU не нужна.
cd /home/user/Agents/Ksenia/pwa
exec /home/user/Agents/Ksenia/.venv/bin/python gateway.py
