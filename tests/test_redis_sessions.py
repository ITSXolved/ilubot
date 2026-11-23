"""
tests/test_redis_sessions.py — Comprehensive tests for Redis sessions, 30m TTL, and WebSocket routing.
"""

import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from fastapi.testclient import TestClient

from core.redis_session import RedisSessionManager, _InMemoryFallback, get_session_manager
from core.conference_server import ConferenceServer


def test_session_manager_crud_and_gemini_format():
    mgr = RedisSessionManager(redis_url=None, ttl_seconds=1800)
    sess_id = "test_user_123"

    # Clean start
    mgr.clear_session(sess_id)
    assert mgr.get_history(sess_id) == []

    # Create session
    mgr.create_or_get_session(sess_id, metadata={"grade": "10", "topic": "Biology"})
    mgr.set_session_language(sess_id, "German")
    assert mgr.get_session_language(sess_id) == "German"

    # Add conversation turns
    mgr.add_message(sess_id, "user", "What is mitochondria?")
    mgr.add_message(sess_id, "model", "What have you observed about where cellular energy comes from?", name="Ilubot")

    history = mgr.get_history(sess_id)
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["text"] == "What is mitochondria?"
    assert history[1]["role"] == "model"
    assert "Ilubot" in history[1]["name"]

    # Test Gemini history format (types.Content)
    gemini_history = mgr.get_gemini_history(sess_id)
    assert len(gemini_history) == 2
    assert gemini_history[0].role == "user"
    assert gemini_history[0].parts[0].text == "What is mitochondria?"
    assert gemini_history[1].role == "model"
    assert "cellular energy" in gemini_history[1].parts[0].text

    # TTL check (should be around 1800s)
    ttl = mgr.get_ttl(sess_id)
    assert 1700 <= ttl <= 1800

    # Clear session
    assert mgr.clear_session(sess_id) is True
    assert mgr.get_history(sess_id) == []
    print("test_session_manager_crud_and_gemini_format: PASSED")


def test_session_30min_auto_expiration():
    # Test expiration using short TTL
    mgr = RedisSessionManager(redis_url=None, ttl_seconds=1)
    sess_id = "expiring_session_test"

    mgr.create_or_get_session(sess_id)
    mgr.add_message(sess_id, "user", "Hello temporary session")
    assert len(mgr.get_history(sess_id)) == 1

    # Sleep past the TTL
    time.sleep(1.2)

    # After expiration, history must be empty and session cleaned automatically
    assert mgr.get_history(sess_id) == []
    assert mgr.get_ttl(sess_id) == -2
    print("test_session_30min_auto_expiration: PASSED")


def test_conference_server_session_endpoints():
    server = ConferenceServer()
    client = TestClient(server.app)

    # Test teaching language with session_id
    sess_id = "student_maria"
    res1 = client.post("/api/teaching_language", json={"language": "French", "session_id": sess_id})
    assert res1.status_code == 200
    assert res1.json()["language"] == "French"

    res2 = client.get(f"/api/teaching_language?session_id={sess_id}")
    assert res2.status_code == 200
    assert res2.json()["language"] == "French"

    # Add message via session manager
    server.session_manager.add_message(sess_id, "user", "Je voudrais apprendre.")
    server.session_manager.add_message(sess_id, "model", "Bienvenue!", name="Ilubot")

    # Query history route
    res3 = client.get(f"/api/sessions/{sess_id}/history")
    assert res3.status_code == 200
    data = res3.json()
    assert data["session_id"] == sess_id
    assert data["message_count"] == 2
    assert data["messages"][0]["text"] == "Je voudrais apprendre."
    assert data["ttl"] > 0

    # Clear session route
    res4 = client.delete(f"/api/sessions/{sess_id}")
    assert res4.status_code == 200
    assert res4.json()["status"] == "cleared"

    res5 = client.get(f"/api/sessions/{sess_id}/history")
    assert res5.json()["message_count"] == 0
    print("test_conference_server_session_endpoints: PASSED")


def test_websocket_session_isolation():
    server = ConferenceServer()
    client = TestClient(server.app)

    received_commands = []
    server.on_text_command = lambda sid, cmd: received_commands.append((sid, cmd))

    # Connect client A (Alice)
    with client.websocket_connect("/ws?session_id=sess_alice") as ws_alice:
        init_a = ws_alice.receive_json()
        assert init_a["type"] == "session_init"
        assert init_a["session_id"] == "sess_alice"

        # Alice sends command
        ws_alice.send_json({"action": "command", "text": "Help with Calculus"})
        time.sleep(0.1)
        assert ("sess_alice", "Help with Calculus") in received_commands

        # Connect client B (Bob)
        with client.websocket_connect("/ws?session_id=sess_bob") as ws_bob:
            init_b = ws_bob.receive_json()
            assert init_b["type"] == "session_init"
            assert init_b["session_id"] == "sess_bob"

            # Bob sends command
            ws_bob.send_json({"action": "command", "text": "Help with History"})
            time.sleep(0.1)
            assert ("sess_bob", "Help with History") in received_commands

    print("test_websocket_session_isolation: PASSED")


if __name__ == "__main__":
    test_session_manager_crud_and_gemini_format()
    test_session_30min_auto_expiration()
    test_conference_server_session_endpoints()
    test_websocket_session_isolation()
    print("ALL TESTS PASSED SUCCESSFULLY!")
