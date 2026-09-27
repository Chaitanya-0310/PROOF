import json
import redis
from dataclasses import asdict
from difflib import SequenceMatcher
from proof.agents.subagent import SubAgentResult

# Initialize Redis client
redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

def get_cached_quality_response(domain: str, question: str) -> SubAgentResult | None:
    key = f"agent_cache:{domain}:{question.strip().lower()}"
    res_str = redis_client.get(key)
    if res_str:
        print(f"CACHE HIT: {key}")
        res = json.loads(res_str)
        return SubAgentResult(**res)
    return None

def set_cached_quality_response(domain: str, question: str, result: SubAgentResult):
    key = f"agent_cache:{domain}:{question.strip().lower()}"
    redis_client.set(key, json.dumps(asdict(result)))
