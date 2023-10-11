import asyncio
import json
import logging
import os
import sys
import requests
import jinja2
from typing import Optional

import yaml
from markdown import markdown
from nio import (
    AsyncClient,
    AsyncClientConfig,
    LoginResponse,
    MatrixRoom,
    RoomMessageText,
    RoomMessageImage,
    SyncResponse,
    UploadResponse,
)
from termcolor import colored


class E2EEClient:
    def __init__(self, join_rooms: set):
        self.STORE_PATH = os.environ["LOGIN_STORE_PATH"]
        self.CONFIG_FILE = f"{self.STORE_PATH}/credentials.json"

        self.join_rooms = join_rooms
        self.client: AsyncClient = None
        self.client_config = AsyncClientConfig(
            max_limit_exceeded=0,
            max_timeouts=0,
            store_sync_tokens=True,
            encryption_enabled=True,
        )

        self.greeting_sent = False

    def _write_details_to_disk(self, resp: LoginResponse, homeserver) -> None:
        with open(self.CONFIG_FILE, "w") as f:
            json.dump(
                {
                    "homeserver": homeserver,  # e.g. "https://matrix.example.org"
                    "user_id": resp.user_id,  # e.g. "@user:example.org"
                    "device_id": resp.device_id,  # device ID, 10 uppercase letters
                    "access_token": resp.access_token,  # cryptogr. access token
                },
                f,
            )

    async def _login_first_time(self) -> None:
        homeserver = os.environ["MATRIX_SERVER"]
        user_id = os.environ["MATRIX_USERID"]
        pw = os.environ["MATRIX_PASSWORD"]
        device_name = os.environ["MATRIX_DEVICE"]

        if not os.path.exists(self.STORE_PATH):
            os.makedirs(self.STORE_PATH)

        self.client = AsyncClient(
            homeserver,
            user_id,
            store_path=self.STORE_PATH,
            config=self.client_config,
            ssl=(os.environ["MATRIX_SSLVERIFY"] == "True"),
        )

        resp = await self.client.login(password=pw, device_name=device_name)

        if isinstance(resp, LoginResponse):
            self._write_details_to_disk(resp, homeserver)
        else:
            logging.info(f'homeserver = "{homeserver}"; user = "{user_id}"')
            logging.critical(f"Failed to log in: {resp}")
            sys.exit(1)

    async def _login_with_stored_config(self) -> None:
        if self.client:
            return

        with open(self.CONFIG_FILE, "r") as f:
            config = json.load(f)

            self.client = AsyncClient(
                config["homeserver"],
                config["user_id"],
                device_id=config["device_id"],
                store_path=self.STORE_PATH,
                config=self.client_config,
                ssl=bool(os.environ["MATRIX_SSLVERIFY"]),
            )

            self.client.restore_login(
                user_id=config["user_id"],
                device_id=config["device_id"],
                access_token=config["access_token"],
            )

    async def login(self) -> None:
        if os.path.exists(self.CONFIG_FILE):
            logging.info("Logging in using stored credentials.")
        else:
            logging.info("First time use, did not find credential file.")
            await self._login_first_time()
            logging.info(
                f"Logged in, credentials are stored under '{self.STORE_PATH}'."
            )

        await self._login_with_stored_config()

    async def _message_callback(self, room: MatrixRoom, event: RoomMessageText) -> None:
        logging.info(
            colored(
                f"@{room.user_name(event.sender)} in {room.display_name} | {event.body}",
                "green",
            )
        )

    async def _sync_callback(self, response: SyncResponse) -> None:
        logging.info(f"We synced, token: {response.next_batch}")

        if not self.greeting_sent:
            self.greeting_sent = True

            content = {
                "msgtype": "m.text",
                "body": f"Hi, I'm up and running from {os.environ['MATRIX_DEVICE']}, and waiting for webhooks!",
                "format": "org.matrix.custom.html",
                "formatted_body": f"Hi, I'm up and running from <b>{os.environ['MATRIX_DEVICE']}</b>, and waiting for webhooks!",
            }

            await self.client.room_send(
                room_id=os.environ["MATRIX_ADMIN_ROOM"],
                message_type="m.room.message",
                content=content,
                ignore_unverified_devices=True,
            )

            # greeting = f"Hi, I'm up and runnig from **{os.environ['MATRIX_DEVICE']}**, waiting for webhooks!"
            # await self.send_message(greeting, os.environ['MATRIX_ADMIN_ROOM'], 'Webhook server')

    async def send_message(
        self, message: str, room: str, sender: str, sync: Optional[bool] = False
    ) -> None:
        if sync:
            await self.client.sync(timeout=3000, full_state=True)

        msg_prefix = ""
        if os.environ["DISPLAY_APP_NAME"] == "True":
            msg_prefix = f"**{sender}** says:  \n"

        content = {
            "msgtype": "m.text",
            "body": f"{msg_prefix}{message}",
            "format": "org.matrix.custom.html",
            "formatted_body": f'Information from the <b>{sender}</b> webhook:\n <pre><code class="language-yaml">{message}</code></pre>',
        }
        if os.environ["USE_MARKDOWN"] == "True":
            # Markdown formatting removes YAML newlines if not padded with spaces,
            # and can also mess up posted data like system logs
            logging.debug("Markdown formatting is turned on.")

            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = markdown(
                f"{msg_prefix}{message}", extensions=["extra"]
            )

        await self.client.room_send(
            room_id=room,
            message_type="m.room.message",
            content=content,
            ignore_unverified_devices=True,
        )

    async def is_image_url(self, url):
        try:
            response = requests.head(url)
            content_type = response.headers.get("Content-Type", "")
            return content_type.startswith("image/")
        except requests.RequestException:
            return False

    async def get_all_values(self, d):
        values = []
        for key, value in d.items():
            if isinstance(value, dict):
                values.extend(self.get_nested_values(value))
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        values.extend(self.get_nested_values(item))
                    else:
                        values.append(item)
            else:
                values.append(value)
        return values

    async def find_image_url(self, payload):
        values = await self.get_all_values(payload)
        for value in values:
            if isinstance(value, str) and await self.is_image_url(value):
                return value
        return None

    async def send_image_to_matrix(self, room: str, payload: dict, source: str):
        if source is not None:
            # Fetch the Jinja2 template
            with open(f"../templates/{source}.jinja2", "r") as f:
                template_json = f.read()

            # Create a Jinja2 environment from the template
            environment = jinja2.Environment()
            template = environment.from_string(template_json)

            # Check if there's an image in the payload
            image_url = await self.find_image_url(payload)
            if image_url:
                # Download the image from the payload
                response = requests.get(image_url)
                image_data = response.content

                content_type = response.headers.get("Content-Type", "")
                mimetype = content_type.split("/")[0]

                # Upload the image to the Matrix content repository
                upload_response: UploadResponse = await self.client.upload(
                    image_data, mimetype
                )
                mxc_uri = upload_response.content_uri
                payload["mxc_uri"] = mxc_uri

            # Render the template with the Slack payload and the MXC URI
            matrix_payload = template.render(payload=payload)

            # Now you can use `matrix_payload` with the Matrix nio package
            room_message = RoomMessageImage(room, matrix_payload)
            response = await self.client.room_send(room_message)

    async def run(self) -> None:
        await self.login()

        self.client.add_event_callback(self._message_callback, RoomMessageText)
        self.client.add_response_callback(self._sync_callback, SyncResponse)

        if self.client.should_upload_keys:
            await self.client.keys_upload()

        for room in self.join_rooms:
            await self.client.join(room)
        await self.client.joined_rooms()

        logging.info("The Matrix client is waiting for events.")

        await self.client.sync_forever(timeout=300000, full_state=True)
