"""A small customer-support agent instrumented with the Mantis SDK.

Uses OpenAI if OPENAI_API_KEY is set, otherwise a deterministic offline model so the demo runs anywhere.
Run:  MANTIS_API_KEY=mk_... python demo/demo_agent.py
"""
import os
import random
import sys
import time

import mantis_sdk

ORDERS = {"A100": "shipped, arriving Friday", "A200": "processing", "A300": "delivered Tuesday"}
CUSTOMERS = [("u-101", "jane.doe@example.com"), ("u-102", "omar@example.org"), ("u-103", "li.wei@example.net")]
QUESTIONS = ["Where is my order A100?", "Status of A200 please", "Did A300 arrive yet?", "Hi, question about my order",
             "Cancel A200 now!!", "My card 4111 1111 1111 1111 was charged twice for A100"]


@mantis_sdk.tool
def lookup_order(order_id: str) -> dict:
    time.sleep(0.02)
    return {"order_id": order_id, "status": ORDERS.get(order_id, "unknown")}


@mantis_sdk.tool
def escalate(reason: str) -> dict:
    return {"ticket": f"T-{random.randint(1000, 9999)}", "reason": reason}


def llm(messages: list[dict], model: str) -> str:
    if os.getenv("OPENAI_API_KEY"):
        import openai
        r = openai.OpenAI().chat.completions.create(model=model, messages=messages, temperature=0)
        return r.choices[0].message.content
    # Offline stand-in, traced explicitly so the span looks like a real generation.
    with mantis_sdk.span(f"chat {model}", kind="llm_generation", **{
        "gen_ai.system": "demo", "gen_ai.request.model": model, "gen_ai.response.model": model,
        "gen_ai.prompt": str(messages)[:2000], "gen_ai.usage.input_tokens": 90 + len(str(messages)) // 4,
        "gen_ai.usage.output_tokens": 30}) as sp:
        time.sleep(random.uniform(0.05, 0.4))
        q = messages[-1]["content"]
        out = (f"Order status: {ORDERS.get(next((o for o in ORDERS if o in q), ''), 'unknown')}." if any(o in q for o in ORDERS)
               else "Could you share your order number?")
        if random.random() < 0.1:
            sp.set_attribute("gen_ai.completion", ""); raise RuntimeError("provider timeout")
        sp.set_attribute("gen_ai.completion", out)
        return out


def handle(question: str, user: str, email: str, session: str, model: str):
    with mantis_sdk.trace("support_agent.run", session_id=session, user_ref=user, **{"customer.email": email}):
        with mantis_sdk.span("policy_check", kind="policy_check", **{"policy": "pii-guard@2"}):
            pass
        order = next((o for o in ORDERS if o in question), None)
        context = lookup_order(order)["status"] if order else None
        if "cancel" in question.lower():
            escalate("cancellation request")
        try:
            return llm([{"role": "system", "content": f"You are a support agent. Known order status: {context}"},
                        {"role": "user", "content": question}], model)
        except RuntimeError:
            return "Sorry, something went wrong."


if __name__ == "__main__":
    mantis_sdk.init(endpoint=os.getenv("MANTIS_ENDPOINT", "http://localhost:8080"), api_key=os.environ["MANTIS_API_KEY"],
                    service_name="support-agent", release=os.getenv("AGENT_RELEASE", "1.0.0"), environment="prod",
                    redact_fields=["customer.email"])
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    model = os.getenv("AGENT_MODEL", "gpt-4o-mini" if os.getenv("OPENAI_API_KEY") else "demo-model-v1")
    for i in range(n):
        user, email = random.choice(CUSTOMERS)
        print(f"[{i+1}/{n}] {user}: {handle(random.choice(QUESTIONS), user, email, f'sess-{i//3}', model)}")
    mantis_sdk.flush(); mantis_sdk.shutdown()
    print("done — open the console to see the traces")
