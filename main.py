import asyncio
import socket
import threading
import time
import json
import uuid
from collections import deque
import websockets
import webbrowser
import networkx as nx
from http.server import SimpleHTTPRequestHandler
import socketserver
import colorama

# --- Constants & Global State ---
PORT, BROADCAST_PORT = 8888, 9999
WEB_PORT, WEBSOCKET_PORT = 8080, 8081

peers = set()
available_peers = {}
peer_lock = threading.Lock()
seen_message_ids = deque(maxlen=200)
connected_peers_info = {}

LOCAL_IP = "" # Will be set after networking starts
USER_NAME = ""
ui_queue = asyncio.Queue()
# --- NEW: Event to signal when the user has registered their name ---
user_registered_event = asyncio.Event()

# --- Core Functions (get_local_ip, format_message are the same) ---
def get_local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s: s.connect(("8.8.8.8", 80)); return s.getsockname()[0]
    except Exception: return "127.0.0.1"

def format_message(sender_ip, message, is_self=False, color=None, hops=0, is_private=False):
    # This function is unchanged
    if color is None: color = colorama.Fore.CYAN
    sender_name = connected_peers_info.get(sender_ip, sender_ip)
    if is_self: sender, color = f"Me ({USER_NAME})", colorama.Fore.GREEN
    elif sender_ip == "System": sender, color = "System", colorama.Fore.YELLOW
    else: sender = sender_name
    hop_info = f" (via {hops} hop{'s' if hops != 1 else ''})" if hops > 0 else ""
    pm_info = "[Private] " if is_private else ""
    return f"{color}{pm_info}{sender}{hop_info}: {colorama.Style.RESET_ALL}{message}"

# --- RoutingManager Class (Unchanged) ---
class RoutingManager:
    # ... (The entire RoutingManager class from the previous code goes here, unchanged) ...
    def __init__(self, local_ip_func, peers_set): self.get_local_ip, self.peers = local_ip_func, peers_set; self.topology = nx.Graph(); self.link_state_seq, self.last_lsa_seq = 0, {}
    def update_topology(self, lsa):
        source, seq = lsa["source"], lsa["seq"];
        if seq > self.last_lsa_seq.get(source, -1):
            self.last_lsa_seq[source] = seq; edges_to_remove = list(self.topology.edges(source)); self.topology.remove_edges_from(edges_to_remove)
            for neighbor, weight in lsa["neighbors"].items(): self.topology.add_edge(source, neighbor, weight=weight)
            return True
        return False
    def get_link_state_packet(self):
        self.link_state_seq += 1; local_ip = self.get_local_ip(); self.topology.add_node(local_ip)
        neighbors = {w.get_extra_info('peername')[0]: 1 for w in self.peers if w.get_extra_info('peername')}
        return {"type": "lsa", "id": str(uuid.uuid4()), "source": local_ip, "seq": self.link_state_seq, "neighbors": neighbors}
    def get_next_hop(self, destination_ip):
        try: path = nx.dijkstra_path(self.topology, self.get_local_ip(), destination_ip); return path[1] if len(path) > 1 else None
        except (nx.NetworkXNoPath, nx.NodeNotFound): return None
    async def broadcast_lsa(self):
        await user_registered_event.wait() # Don't start broadcasting until name is set
        while True:
            await asyncio.sleep(10); lsa_packet = self.get_link_state_packet(); seen_message_ids.append(lsa_packet["id"])
            lsa_json = json.dumps(lsa_packet) + "\n"
            for writer in list(self.peers):
                try: writer.write(lsa_json.encode()); await writer.drain()
                except: self.peers.discard(writer)

# --- Connection Management ---
async def manage_connection(reader, writer, routing_manager):
    peer_ip = writer.get_extra_info('peername')[0]
    try:
        handshake = {"type": "handshake", "name": USER_NAME}
        writer.write((json.dumps(handshake) + "\n").encode()); await writer.drain()
        
        while True:
            line = await reader.readline()
            if not line: break
            try:
                message_data = json.loads(line.decode()); msg_id = message_data.get("id")
                if msg_id and msg_id in seen_message_ids: continue
                if msg_id: seen_message_ids.append(msg_id)
                msg_type = message_data.get("type")
                if msg_type == "handshake":
                    peer_name = message_data.get("name", "Unknown"); connected_peers_info[peer_ip] = peer_name
                    peers.add(writer); await ui_queue.put({"action": "system", "message": f"🔗 {peer_name} ({peer_ip}) connected."})
                    await ui_queue.put({"action": "connected_peer_list", "peers": [{ "ip": ip, "name": name } for ip, name in connected_peers_info.items()]})
                elif msg_type == "lsa":
                    if routing_manager.update_topology(message_data):
                        for p_writer in list(peers):
                            if p_writer != writer: p_writer.write(line); await p_writer.drain()
                elif msg_type == "chat":
                    dest = message_data.get("destination_ip")
                    if dest == routing_manager.get_local_ip() or dest == "all":
                        await ui_queue.put({"action": "new_message", "sender": message_data["origin_ip"], "payload": message_data["payload"], "is_private": (dest != "all")})
                    if dest != routing_manager.get_local_ip():
                        message_data["hops"] += 1; forward_msg = json.dumps(message_data) + "\n"
                        if dest == "all":
                            for p_writer in list(peers):
                                if p_writer != writer: p_writer.write(forward_msg.encode()); await p_writer.drain()
                        else:
                            next_hop = routing_manager.get_next_hop(dest)
                            if next_hop:
                                for p_writer in list(peers):
                                    p_info = p_writer.get_extra_info('peername')
                                    if p_info and p_info[0] == next_hop: p_writer.write(forward_msg.encode()); await p_writer.drain(); break
            except: pass
    except: pass
    finally:
        peer_name = connected_peers_info.pop(peer_ip, peer_ip)
        await ui_queue.put({"action": "system", "message": f"🏃 {peer_name} disconnected."})
        if writer in peers: peers.remove(writer)
        if routing_manager.topology.has_node(peer_ip): routing_manager.topology.remove_node(peer_ip)
        if not writer.is_closing(): writer.close(); await writer.wait_closed()
        await ui_queue.put({"action": "connected_peer_list", "peers": [{ "ip": ip, "name": name } for ip, name in connected_peers_info.items()]})

async def connect_to_peer(ip, routing_manager):
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, PORT), timeout=5)
        asyncio.create_task(manage_connection(reader, writer, routing_manager))
    except Exception as e:
        await ui_queue.put({"action": "error", "message": f"Failed to connect to {ip}: {e}"})

async def handle_peer(reader, writer, routing_manager):
    asyncio.create_task(manage_connection(reader, writer, routing_manager))

# --- UDP Discovery Threads ---
async def udp_broadcast_task():
    await user_registered_event.wait() # Don't broadcast until name is set
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while True:
            try:
                message = f"{USER_NAME}|{LOCAL_IP}"; sock.sendto(message.encode(), ("255.255.255.255", BROADCAST_PORT))
            except: pass
            time.sleep(5)
def udp_listen_task():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); sock.bind(('', BROADCAST_PORT))
        while True:
            try:
                data, addr = sock.recvfrom(1024); name, ip = data.decode().split('|', 1)
                if ip != LOCAL_IP:
                    with peer_lock: available_peers[ip] = name
            except: pass

# --- WebSocket Bridge to Frontend ---
async def web_handler(websocket, routing_manager):
    global USER_NAME
    
    async def to_ui():
        while True:
            message = await ui_queue.get(); await websocket.send(json.dumps(message))
    
    async def from_ui():
        async for message in websocket:
            data = json.loads(message)
            action = data.get("action")
            
            if action == "register":
                USER_NAME = data.get("name", "Anonymous")
                connected_peers_info[LOCAL_IP] = USER_NAME
                user_registered_event.set() # Signal that the name is set
                await websocket.send(json.dumps({"action": "set_user_info", "name": USER_NAME, "ip": LOCAL_IP}))

            elif action == "get_peers":
                with peer_lock: peer_list = [{"ip": ip, "name": name} for ip, name in available_peers.items()]
                await ui_queue.put({"action": "available_peer_list", "peers": peer_list})

            elif action == "connect": asyncio.create_task(connect_to_peer(data["ip"], routing_manager))

            elif action == "disconnect":
                ip_to_disconnect = data["ip"]
                for writer in list(peers):
                    peer_info = writer.get_extra_info('peername')
                    if peer_info and peer_info[0] == ip_to_disconnect:
                        writer.close(); await writer.wait_closed(); break

            elif action == "send_message":
                msg_id = str(uuid.uuid4())
                message_data = {"type": "chat", "id": msg_id, "origin_ip": LOCAL_IP, "destination_ip": "all", "hops": 0, "payload": data["payload"]}
                seen_message_ids.append(msg_id)
                for writer in list(peers): writer.write((json.dumps(message_data) + "\n").encode()); await writer.drain()

    await asyncio.gather(to_ui(), from_ui())

# --- Main Application ---
async def main_async():
    global LOCAL_IP
    LOCAL_IP = get_local_ip()
    print("--- P2P Mesh Chat Backend ---")
    
    routing_manager = RoutingManager(lambda: LOCAL_IP, peers)
    
    p2p_server = await asyncio.start_server(
        lambda r, w: handle_peer(r, w, routing_manager), LOCAL_IP, PORT)
    
    web_server = await websockets.serve(
        lambda ws: web_handler(ws, routing_manager), "localhost", WEBSOCKET_PORT)
    print(f"[Info] WebSocket bridge running on ws://localhost:{WEBSOCKET_PORT}")

    loop = asyncio.get_running_loop()
    loop.create_task(udp_broadcast_task()) # Start broadcast task, it will wait on the event
    loop.run_in_executor(None, udp_listen_task)
    
    asyncio.create_task(routing_manager.broadcast_lsa())
    
    print(f"[Info] Open http://localhost:{WEB_PORT} in your browser to start.")
    webbrowser.open(f'http://localhost:{WEB_PORT}')
    
    await asyncio.gather(p2p_server.serve_forever(), web_server.wait_closed())

if __name__ == "__main__":
    Handler = SimpleHTTPRequestHandler
    httpd = socketserver.TCPServer(("", WEB_PORT), Handler)
    
    print(f"Serving HTML on http://localhost:{WEB_PORT}")
    http_thread = threading.Thread(target=httpd.serve_forever); http_thread.daemon = True; http_thread.start()
    
    try: asyncio.run(main_async())
    except KeyboardInterrupt: print("\nExiting...")
    finally: httpd.shutdown()