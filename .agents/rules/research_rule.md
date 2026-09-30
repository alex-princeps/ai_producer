# Project Aristarkh: Security & Resource Management Rules

## 1. Do not read raw research papers or lectures
You are EXPLICITLY FORBIDDEN from directly reading, viewing, or parsing any documents (PDFs, DOCX, TXT, etc.) located in the `research_papers/` or `knowledge_base/` directories during regular code work or task execution.

## 2. Why this rule exists
Reading raw academic papers or lectures directly into the context window consumes an enormous amount of tokens (often hundreds of thousands of tokens per file), which is inefficient and expensive.

## 3. How to access research data
All access to the scientific knowledge contained in `research_papers/` or `knowledge_base/` MUST be done exclusively through the dedicated local RAG scripts (`tools/research_rag.py` or `aristarkh_core/rag_chroma.py`). You must query the RAG database to retrieve only the most relevant chunks of information.
