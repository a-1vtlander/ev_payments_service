"""
MQTT client factory.

Builds a paho Client wired up to the shared state queues.  The caller
(lifespan.py) passes the list of topics to subscribe; on_message routes
each incoming message to the matching asyncio.Queue in state._topic_queues.
"""

import logging

import paho.mqtt.client as mqtt

import state

log = logging.getLogger(__name__)


def build_mqtt_client(mqtt_cfg: dict, subscribed_topics: list) -> mqtt.Client:
    """
    Build and configure a paho MQTT client.

    mqtt_cfg – structured dict with keys: host, port, username, password.
    subscribed_topics – topics to subscribe to in on_connect (so subscriptions
                        survive broker reconnects).
    """
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    username = (mqtt_cfg.get("username") or "").strip()
    password = (mqtt_cfg.get("password") or "").strip()
    if username:
        client.username_pw_set(username, password or None)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("MQTT connected to %s:%s", mqtt_cfg.get("host"), mqtt_cfg.get("port", 1883))
            for topic in subscribed_topics:
                client.subscribe(topic, qos=1)
                log.info("Subscribed to: %s", topic)
        else:
            log.warning("MQTT connect failed, reason code: %s", reason_code)

    def on_disconnect(client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            log.warning(
                "MQTT unexpectedly disconnected (reason=%s) – will auto-reconnect", reason_code
            )
        else:
            log.info("MQTT disconnected cleanly")

    def on_publish(client, userdata, mid, reason_code, properties):
        log.info("MQTT message published (mid=%s)", mid)

    def on_message(client, userdata, message):
        """Route incoming messages to the asyncio queue registered for that topic."""
        payload = message.payload.decode(errors="replace")
        log.info("Received MQTT message on %s: %s", message.topic, payload)
        queue = state._topic_queues.get(message.topic)
        if queue is None:
            log.warning("No queue registered for topic %s – dropping message", message.topic)
            return
        if state._event_loop:
            state._event_loop.call_soon_threadsafe(queue.put_nowait, payload)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_publish = on_publish
    client.on_message = on_message

    # Exponential back-off between 1 s and 60 s reconnect attempts.
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    return client
