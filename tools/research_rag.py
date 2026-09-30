import os
import sys
import argparse
import asyncio
import aiohttp
import chromadb
import fitz
import docx
import shutil
from dotenv import load_dotenv

# Load .env from the root directory
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, '.env'))

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    print("Error: GEMINI_API_KEY not found in .env file.")
    sys.exit(1)

# STRICTLY enforced model as per user constraints
EMBEDDING_MODEL = "models/gemini-embedding-001"

RESEARCH_PAPERS_DIR = os.path.join(BASE_DIR, "research_papers")
CHROMA_DB_PATH = os.path.join(BASE_DIR, "tools", "research_chroma_db")

class SmartTextSplitter:
    """
    Custom recursive text splitter.
    """
    def __init__(self, chunk_size=1500, chunk_overlap=300):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.separators = ["\n\n", "\n", ". ", "? ", "! ", " "]

    def split_text(self, text: str) -> list[str]:
        return self._split_recursive(text, self.separators)

    def _split_recursive(self, text: str, separators: list[str]) -> list[str]:
        final_chunks = []
        if len(text) <= self.chunk_size:
            return [text]

        separator = separators[0]
        for s in separators:
            if s in text:
                separator = s
                break
        
        splits = text.split(separator)
        good_splits = []
        
        for s in splits:
            if len(s) < self.chunk_size:
                good_splits.append(s)
            else:
                if good_splits:
                    final_chunks.extend(self._merge_splits(good_splits, separator))
                    good_splits = []
                next_separators = separators[separators.index(separator) + 1:] if separator in separators else []
                if next_separators:
                    final_chunks.extend(self._split_recursive(s, next_separators))
                else:
                    final_chunks.extend([s[i:i+self.chunk_size] for i in range(0, len(s), self.chunk_size)])
                    
        if good_splits:
            final_chunks.extend(self._merge_splits(good_splits, separator))
            
        return final_chunks

    def _merge_splits(self, splits: list[str], separator: str) -> list[str]:
        docs = []
        current_doc = []
        total_len = 0
        
        for s in splits:
            _len = len(s) + (len(separator) if current_doc else 0)
            if total_len + _len > self.chunk_size and current_doc:
                docs.append(separator.join(current_doc))
                while total_len > self.chunk_overlap and len(current_doc) > 1:
                    total_len -= len(current_doc[0]) + len(separator)
                    current_doc.pop(0)
            current_doc.append(s)
            total_len += _len
            
        if current_doc:
            docs.append(separator.join(current_doc))
        return docs

class ResearchRAG:
    def __init__(self):
        os.makedirs(RESEARCH_PAPERS_DIR, exist_ok=True)
        os.makedirs(CHROMA_DB_PATH, exist_ok=True)
        print("Initializing ResearchRAG...")
        print("Creating Chroma client...")
        self.chroma_client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
        print("Getting collection...")
        self.collection = self.chroma_client.get_or_create_collection(name="research_papers")
        print("Creating splitter...")
        self.splitter = SmartTextSplitter()
        print("Initialization complete.")
        self.http_session = None

    def _read_file(self, filepath):
        if not os.path.exists(filepath): return ""
        try:
            if filepath.endswith('.pdf'):
                doc = fitz.open(filepath)
                return "".join([page.get_text() for page in doc])
            elif filepath.endswith('.docx'):
                doc = docx.Document(filepath)
                return "\n".join([p.text for p in doc.paragraphs])
            elif filepath.endswith('.txt') or filepath.endswith('.md'):
                with open(filepath, 'r', encoding='utf-8') as f: return f.read()
        except Exception as e:
            print(f"Error reading {filepath}: {e}")
            return ""
        return ""

    async def _get_embedding(self, text: str):
        url = f"https://generativelanguage.googleapis.com/v1beta/{EMBEDDING_MODEL}:embedContent?key={GEMINI_API_KEY}"
        # Truncate text to avoid exceeding limits
        payload = {"model": EMBEDDING_MODEL, "content": {"parts": [{"text": text[:2000]}]}}
        
        session = self.http_session or aiohttp.ClientSession(trust_env=True)
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data['embedding']['values']
                else:
                    print(f"Embedding API Error: {resp.status} - {await resp.text()}")
                    return None
        except Exception as e:
            print(f"Embedding Conn Error: {e}")
            return None
        finally:
            if not self.http_session:
                await session.close()

    async def index_documents(self):
        print(f"Scanning {RESEARCH_PAPERS_DIR}...", flush=True)
        files = [f for f in os.listdir(RESEARCH_PAPERS_DIR) if f.endswith(('.pdf', '.docx', '.txt', '.md'))]
        if not files:
            print("No documents found to index.", flush=True)
            return

        existing_ids = set()
        if self.collection.count() > 0:
            res = self.collection.get(include=[])
            existing_ids = set(res['ids'])
            
        for filename in files:
            filepath = os.path.join(RESEARCH_PAPERS_DIR, filename)
            # Basic check to avoid re-indexing already indexed files
            first_chunk_id = f"{filename}_chunk_0"
            if first_chunk_id in existing_ids:
                print(f"Skipping {filename}, already indexed.", flush=True)
                continue

            print(f"Indexing {filename}...", flush=True)
            txt = self._read_file(filepath)
            if len(txt) < 50:
                continue
            
            clean_name = os.path.splitext(filename)[0].replace('_', ' ')
            chunks = self.splitter.split_text(txt)
            print(f"  > Extracted {len(chunks)} chunks. Generating embeddings...", flush=True)
            
            all_ids, all_docs, all_embeddings, all_metadatas = [], [], [], []
            batch_size = 20
            
            for i in range(0, len(chunks), batch_size):
                batch_chunks = chunks[i:i+batch_size]
                tasks = []
                for j, chunk in enumerate(batch_chunks):
                    enriched_chunk = f"[SOURCE: {clean_name}]\n{chunk.strip()}"
                    tasks.append(self._get_embedding(enriched_chunk))
                
                vecs = await asyncio.gather(*tasks)
                
                for j, vec in enumerate(vecs):
                    if vec:
                        doc_id = f"{filename}_chunk_{i+j}"
                        enriched_chunk = f"[SOURCE: {clean_name}]\n{batch_chunks[j].strip()}"
                        all_ids.append(doc_id)
                        all_docs.append(enriched_chunk)
                        all_embeddings.append(vec)
                        all_metadatas.append({"source": filename, "chunk_idx": i+j})
                
                if all_ids:
                    self.collection.add(
                        documents=all_docs, embeddings=all_embeddings, ids=all_ids, metadatas=all_metadatas
                    )
                    all_ids, all_docs, all_embeddings, all_metadatas = [], [], [], []
                
                print(f"  > Processed {min(i+batch_size, len(chunks))}/{len(chunks)} chunks", flush=True)
                await asyncio.sleep(1.2) # Rate limit protection

        print(f"Indexing complete. Total chunks in database: {self.collection.count()}", flush=True)

    def _long_context_reorder(self, documents: list[str]) -> list[str]:
        """
        SOTA Lost in the Middle mitigation.
        Reorders documents so that the most relevant ones are placed at the beginning and end.
        Example for 5 items (ordered by relevance 1 to 5):
        [2, 4, 5, 3, 1]
        """
        if not documents:
            return []
        
        documents.reverse()
        reordered = []
        for i, doc in enumerate(documents):
            if i % 2 == 1:
                reordered.append(doc)
            else:
                reordered.insert(0, doc)
        return reordered

    async def search(self, query: str, top_k: int = 15):
        if self.collection.count() == 0:
            print("Database is empty. Please run 'index' first.")
            return

        print(f"Searching for: '{query}'")
        query_vec = await self._get_embedding(query)
        if not query_vec:
            print("Failed to embed query.")
            return

        results = self.collection.query(
            query_embeddings=[query_vec],
            n_results=min(top_k, self.collection.count()),
            include=["documents", "metadatas", "distances"]
        )

        if not results or not results['documents'] or not results['documents'][0]:
            print("No results found.")
            return

        docs = results['documents'][0]
        
        # Apply Long Context Reordering
        reordered_docs = self._long_context_reorder(docs)
        
        print("\n--- SEARCH RESULTS (REORDERED FOR SOTA LOST-IN-THE-MIDDLE MITIGATION) ---\n")
        print("\n\n==== NEXT FRAGMENT ====\n\n".join(reordered_docs))
        print("\n--- END OF RESULTS ---\n")

async def main():
    parser = argparse.ArgumentParser(description="Research Papers RAG CLI Tool")
    parser.add_argument("command", choices=["index", "search"], help="Command to run: 'index' to process papers, 'search' to query the RAG")
    parser.add_argument("query", nargs="?", default="", help="The search query (required for 'search' command)")
    
    args = parser.parse_args()
    
    rag = ResearchRAG()
    
    if args.command == "index":
        await rag.index_documents()
    elif args.command == "search":
        if not args.query:
            print("Error: Search query is required. Usage: python research_rag.py search \"your query\"")
            sys.exit(1)
        await rag.search(args.query)

if __name__ == "__main__":
    asyncio.run(main())
