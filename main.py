import streamlit as st
import asyncio
import socket
import threading
import time
import json
import uuid
from collections import deque
import networkx as nx

# --- Constants ---
PORT = 8888
BROADCAST_PORT = 9999

# --- Session State Setup ---
if 'peers' not in st.session_state:
    st.session_state.peers = set()
if 'available_peers' not in st.session_state:
    st.session_state.available_peers = {}  # {ip: name}
if 'connected_peers_info' not in st.session_state:
    st.session_state.connected_peers_info = {}  # {ip: name}
if 'seen_message_ids' not in st.session_state:
    st.session_state.seen_message_ids = deque(maxlen=200)
if 'messages' not in st.session_state:
    st.session_state.messages = []
if 'local_ip' not in st.session_state:
    def get_local_ip():
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except:
            return "127.0.0.1"
    st.session_state.local_ip = get_local_ip()
if 'user_name' not in st.session_state:
    st.session_state.user_name = ""

# --- Routing Manager ---
class RoutingManager:
    def __init__(self, local_ip):
        self.local_ip = local_ip
        self.topology = nx.Graph()
        self.topology.add_node(local_ip)
        self.last_lsa_seq = {}
        self.seq = 0

    def get_link_state_packet(self):
        self.seq += 1
        neighbors = {ip: 1 for ip in st.session_state.connected_peers_info if ip != self.local_ip}
        return {"type": "lsa", "id": str(uuid.uuid4()), "source": self.local_ip, "seq": self.seq, "neighbors": neighbors}

    def update_topology(self, lsa):
        source, seq = lsa["source"], lsa["seq"]
        if seq > self.last_lsa_seq.get(source, -1):
            self.last_lsa_seq[source] = seq
            self.topology.add_node(source)
            self.topology.remove_edges_from(list(self.topology.edges(source)))
            for neighbor, weight in lsa["neighbors"].items():
                self.topology.add_edge(source, neighbor, weight=weight)
            return True
        return False

    def get_next_hop(self, destination_ip):
        try:
            path = nx.dijkstra_path(self.topology, self.local_ip, destination_ip)
            return path[1] if len(path) > 1 else None
        except:
            return None

routing_manager = RoutingManager(st.session_state.local_ip)

# --- Networking Code ---
def udp_broadcast():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while True:
            try:
                msg = f"{st.session_state.user_name}|{st.session_state.local_ip}"
                sock.sendto(msg.encode(), ("255.255.255.255", BROADCAST_PORT))
            except:
                pass
            time.sleep(5)

def udp_listen():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('', BROADCAST_PORT))
        while True:
            try:
                data, addr = sock.recvfrom(1024)
                name, ip = data.decode().split('|', 1)
                if ip != st.session_state.local_ip:
                    st.session_state.available_peers[ip] = name
            except:
                pass

async def handle_peer(reader, writer):
    peer_ip = writer.get_extra_info('peername')[0]
    try:
        handshake = {"type": "handshake", "name": st.session_state.user_name}
        writer.write((json.dumps(handshake) + "\n").encode())
        await writer.drain()

        while True:
            line = await reader.readline()
            if not line:
                break
            message_data = json.loads(line.decode())
            msg_id = message_data.get("id")
            if msg_id and msg_id in st.session_state.seen_message_ids:
                continue
            if msg_id:
                st.session_state.seen_message_ids.append(msg_id)

            msg_type = message_data.get("type")
            if msg_type == "handshake":
                peer_name = message_data.get("name", "Unknown")
                st.session_state.connected_peers_info[peer_ip] = peer_name
                st.session_state.peers.add(writer)
                st.session_state.messages.append(f"🟢 {peer_name} connected from {peer_ip}")
            elif msg_type == "lsa":
                if routing_manager.update_topology(message_data):
                    for p_writer in list(st.session_state.peers):
                        if p_writer != writer:
                            try:
                                p_writer.write(line)
                                await p_writer.drain()
                            except:
                                st.session_state.peers.remove(p_writer)
            elif msg_type == "chat":
                dest = message_data.get("destination_ip")
                if dest == routing_manager.local_ip or dest == "all":
                    origin = message_data.get("origin_ip")
                    text = message_data.get("payload")
                    st.session_state.messages.append(f"💬 {origin}: {text}")
                if dest != routing_manager.local_ip:
                    message_data["hops"] += 1
                    forward_msg = json.dumps(message_data) + "\n"
                    if dest == "all":
                        for p_writer in list(st.session_state.peers):
                            if p_writer != writer:
                                try:
                                    p_writer.write(forward_msg.encode())
                                    await p_writer.drain()
                                except:
                                    st.session_state.peers.remove(p_writer)
                    else:
                        next_hop = routing_manager.get_next_hop(dest)
                        if next_hop:
                            for p_writer in list(st.session_state.peers):
                                p_info = p_writer.get_extra_info('peername')
                                if p_info and p_info[0] == next_hop:
                                    try:
                                        p_writer.write(forward_msg.encode())
                                        await p_writer.drain()
                                    except:
                                        st.session_state.peers.remove(p_writer)
                                    break
    except:
        pass
    finally:
        peer_name = st.session_state.connected_peers_info.pop(peer_ip, peer_ip)
        st.session_state.messages.append(f"🔴 {peer_name} disconnected.")
        if writer in st.session_state.peers:
            st.session_state.peers.remove(writer)
        if not writer.is_closing():
            writer.close()
            await writer.wait_closed()

async def start_server():
    server = await asyncio.start_server(handle_peer, st.session_state.local_ip, PORT)
    await server.serve_forever()

def start_asyncio_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_server())

# --- Streamlit UI ---
def main():
    st.title("🌐 P2P Mesh Chat with Dijkstra Routing")

    if not st.session_state.user_name:
        st.session_state.user_name = st.text_input("Enter your name")
        return

    st.write(f"Your IP: {st.session_state.local_ip}")
    st.write(f"Hello, {st.session_state.user_name}!")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Available Peers")
        for ip, name in st.session_state.available_peers.items():
            if st.button(f"Connect to {name} ({ip})", key=f"connect_{ip}"):
                asyncio.run(connect_to_peer(ip))

    with col2:
        st.subheader("Network Map")
        nodes = routing_manager.topology.nodes()
        edges = routing_manager.topology.edges()
        st.write("Nodes:", list(nodes))
        st.write("Edges:", list(edges))

    st.subheader("Messages")
    for msg in st.session_state.messages[-20:]:
        st.write(msg)

    st.subheader("Send Message")
    msg_text = st.text_input("Enter message")
    if st.button("Send"):
        send_message(msg_text)

def send_message(text):
    msg_id = str(uuid.uuid4())
    message_data = {
        "type": "chat",
        "id": msg_id,
        "origin_ip": st.session_state.local_ip,
        "destination_ip": "all",
        "hops": 0,
        "payload": text
    }
    st.session_state.seen_message_ids.append(msg_id)
    st.session_state.messages.append(f"✅ Me: {text}")
    msg_json = json.dumps(message_data) + "\n"
    for writer in list(st.session_state.peers):
        try:
            writer.write(msg_json.encode())
            asyncio.run(writer.drain())
        except:
            st.session_state.peers.remove(writer)

async def connect_to_peer(ip):
    try:
        reader, writer = await asyncio.open_connection(ip, PORT)
        asyncio.create_task(handle_peer(reader, writer))
    except Exception as e:
        st.session_state.messages.append(f"❌ Failed to connect to {ip}: {e}")

# --- Start Background Tasks ---
if __name__ == "__main__":
    threading.Thread(target=udp_broadcast, daemon=True).start()
    threading.Thread(target=udp_listen, daemon=True).start()
    threading.Thread(target=start_asyncio_loop, daemon=True).start()
    main()
