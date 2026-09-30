import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

os.environ["SUPABASE_DB_URL"]=""
os.environ["DATABASE_URL"]=""

from app import app as api_app, integrations as integration_status
from core import Column, Config, InsufficientCreditsError, PostgresConnectionAdapter, analyze_csv, analyze_prompt, connection, credit_cost, document_export_bytes, explain_validation, export_bytes, generate_relational, generate_tabular, get_credit_balance, get_dataset, init_db, list_datasets, list_versions, make_document, privacy_scan, save_dataset, save_version, validate
from fastapi.testclient import TestClient


class GenerationTests(unittest.TestCase):
    def test_supabase_connection_requires_tls_and_supports_transaction_pooler(self):
        with patch.dict("os.environ",{"SUPABASE_DB_URL":"postgresql://admin:secret@example.test/db"},clear=True), patch("psycopg.connect") as connect_db:
            with connection() as db:
                self.assertIsInstance(db,PostgresConnectionAdapter)
        self.assertEqual(connect_db.call_args.args,("postgresql://admin:secret@example.test/db",))
        self.assertEqual(connect_db.call_args.kwargs["sslmode"],"require")
        self.assertIsNone(connect_db.call_args.kwargs["prepare_threshold"])
        connect_db.return_value.commit.assert_called_once()
        connect_db.return_value.close.assert_called_once()

    def test_supabase_connection_preserves_certificate_verification(self):
        url="postgresql://admin:secret@example.test/db?sslmode=verify-full&sslrootcert=C%3A%2Fcert.pem"
        with patch.dict("os.environ",{"SUPABASE_DB_URL":url},clear=True), patch("psycopg.connect") as connect_db:
            with connection():
                pass
        self.assertNotIn("sslmode",connect_db.call_args.kwargs)

    def test_supabase_tables_are_closed_to_browser_roles(self):
        with patch.dict("core.os.environ",{"SUPABASE_DB_URL":"postgresql://example.test/db"},clear=True), patch("core.connection") as connect_db:
            init_db()
        execute=connect_db.return_value.__enter__.return_value.execute
        statements=[call.args[0] for call in execute.call_args_list]
        self.assertIn("ALTER TABLE datasets ENABLE ROW LEVEL SECURITY",statements)
        self.assertIn("ALTER TABLE versions ENABLE ROW LEVEL SECURITY",statements)
        self.assertIn("REVOKE ALL ON TABLE datasets, versions, credit_accounts, credit_ledger FROM anon, authenticated",statements)

    def test_postgres_adapter_maps_placeholders_and_write_lock(self):
        class RecordingConnection:
            def execute(self, statement, parameters=()):
                self.statement=statement
                self.parameters=parameters
                return statement

        raw=RecordingConnection()
        adapter=PostgresConnectionAdapter(raw)
        adapter.execute("SELECT id FROM datasets WHERE id=?",("dataset-id",))
        self.assertEqual(raw.statement,"SELECT id FROM datasets WHERE id=%s")
        self.assertEqual(raw.parameters,("dataset-id",))
        adapter.execute("BEGIN IMMEDIATE")
        self.assertEqual(raw.statement,"BEGIN")

    def test_integration_status_does_not_return_database_url(self):
        with patch.dict("os.environ",{"SUPABASE_DB_URL":"postgresql://admin:secret@example.test/db"},clear=True):
            status=integration_status()
        self.assertEqual(status["database"],{"provider":"supabase-postgres","configured":True})
        self.assertNotIn("secret",json.dumps(status))

    def test_api_rejects_missing_or_invalid_access_tokens(self):
        client=TestClient(api_app)
        self.assertEqual(client.get("/api/health").status_code,200)
        missing_token=client.get("/api/datasets")
        self.assertEqual(missing_token.status_code,401)
        self.assertEqual(missing_token.json()["detail"],"Sign in to access this API")
        with patch("app.validate_supabase_access_token",return_value=False):
            response=client.get("/api/datasets",headers={"Authorization":"Bearer expired-token"})
        self.assertEqual(response.status_code,401)

    def test_billing_grants_and_charges_user_credits(self):
        self.assertEqual([credit_cost(rows) for rows in (0,1,1000,1001)],[1,1,1,2])
        config=Config(name="Credit test",rows=1001)
        with tempfile.TemporaryDirectory() as temp, patch("core.DB_PATH",Path(temp)/"credits.db"), patch.dict("core.os.environ",{"SUPABASE_DB_URL":"","DATABASE_URL":""}):
            init_db()
            self.assertEqual(get_credit_balance("user-a"),100)
            self.assertEqual(get_credit_balance("user-a"),100)
            self.assertEqual(get_credit_balance("user-b"),100)
            save_dataset(config,{}, {}, {},user_id="user-a")
            self.assertEqual(get_credit_balance("user-a"),98)
            with self.assertRaises(InsufficientCreditsError):
                save_dataset(config.model_copy(update={"rows":100000}),{}, {}, {},user_id="user-a")
            self.assertEqual(get_credit_balance("user-a"),98)

    def test_billing_api_and_generation_enforce_credit_balance(self):
        user_id="billing-api-user"
        config=Config(name="Credit API",rows=1001,columns=[Column(name="record_id",type="id",key="PK")])
        headers={"Authorization":"Bearer valid-token"}
        with tempfile.TemporaryDirectory() as temp, patch("core.DB_PATH",Path(temp)/"billing-api.db"), patch.dict("core.os.environ",{"SUPABASE_DB_URL":"","DATABASE_URL":""}):
            init_db()
            client=TestClient(api_app)
            with patch("app.validate_supabase_access_token",return_value=user_id):
                billing=client.get("/api/billing",headers=headers)
                self.assertEqual(billing.status_code,200)
                self.assertEqual(billing.json()["balance"],100)
                self.assertFalse(billing.json()["checkout_ready"])
                self.assertEqual([(p["name"],p["price_pkr"],p["credits"]) for p in billing.json()["plans"]],[
                    ("Starter",500,500),("Pro",1500,2000),("Premium",3000,5000)
                ])
                generated=client.post("/api/generate",json=config.model_dump(mode="json"),headers=headers)
                self.assertEqual(generated.status_code,200)
                self.assertEqual(generated.json()["credits_charged"],2)
                self.assertEqual(generated.json()["credits_remaining"],98)
                with connection() as db:
                    db.execute("UPDATE credit_accounts SET balance=1 WHERE user_id=?",(user_id,))
                insufficient=client.post("/api/generate",json=config.model_dump(mode="json"),headers=headers)
                self.assertEqual(insufficient.status_code,402)
                self.assertEqual(get_credit_balance(user_id),1)

    def test_dataset_access_is_limited_to_its_owner(self):
        config=Config(name="Private dataset",rows=10)
        quality={"score":100}
        tables={"records":[{"record_id":"R-1"}]}
        with tempfile.TemporaryDirectory() as temp, patch("core.DB_PATH",Path(temp)/"owners.db"), patch.dict("core.os.environ",{"SUPABASE_DB_URL":"","DATABASE_URL":""}):
            init_db()
            ident,_=save_dataset(config,tables,quality,{"blocked":False},user_id="user-a")
            self.assertIsNotNone(get_dataset(ident,user_id="user-a"))
            self.assertIsNone(get_dataset(ident,user_id="user-b"))
            self.assertEqual(list_datasets("user-b"),[])
            self.assertEqual(list_versions(ident,"user-b"),[])
            with self.assertRaisesRegex(ValueError,"Dataset not found"):
                save_version(ident,config,tables,quality,{"blocked":False},user_id="user-b")
            with patch("app.validate_supabase_access_token",return_value="user-b"):
                client=TestClient(api_app)
                headers={"Authorization":"Bearer valid-token"}
                self.assertEqual(client.get("/api/datasets",headers=headers).json(),[])
                for path in (f"/api/datasets/{ident}",f"/api/datasets/{ident}/versions",f"/api/datasets/{ident}/rows?table=records",f"/api/datasets/{ident}/export/json"):
                    self.assertEqual(client.get(path,headers=headers).status_code,404)

    def test_validation_explanation_endpoint_uses_stored_quality(self):
        stored={"quality":{"score":91,"checks":[{"name":"IDs unique","passed":True}]}}
        answer={"summary":"The measured checks pass.","priorities":[],"model":"test/model"}
        with patch("app.validate_supabase_access_token",return_value=True), patch("app.get_dataset",return_value=stored), patch("app.explain_validation",return_value=answer) as explain:
            response=TestClient(api_app).post("/api/datasets/dataset-id/explain-validation?version=2",headers={"Authorization":"Bearer valid-token"})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),answer)
        explain.assert_called_once_with(stored["quality"])

    def test_validation_explanation_uses_only_aggregate_report(self):
        answer={"summary":"Completeness is the lowest measured dimension.","priorities":["Review configured null rates.","Inspect failed business rules."]}
        response=io.BytesIO(json.dumps({"choices":[{"message":{"content":json.dumps(answer)}}]}).encode())
        report={"score":84,"dimensions":{"Completeness":70},"checks":[{"name":"Age >= 18","passed":False,"detail":"2 violations"}],"counts":{"rows":20,"missing":6},"comparison":[],"rows":[{"name":"must not be sent"}]}
        with patch.dict("core.os.environ",{"OPENROUTER_API_KEY":"test-key","OPENROUTER_MODEL":"test/model"}), patch("core.urllib.request.urlopen",return_value=response) as openrouter:
            result=explain_validation(report)
        self.assertEqual(result["summary"],answer["summary"])
        self.assertEqual(result["priorities"],answer["priorities"])
        request_payload=json.loads(openrouter.call_args.args[0].data)
        self.assertNotIn("must not be sent",request_payload["messages"][1]["content"])
        self.assertEqual(request_payload["max_tokens"],500)

    def test_csv_analysis_infers_key_roles_and_sensitive_aliases(self):
        content = b"product_id,customer_id,email,phone,salary,home_address,bank_account\nP-1,C-1,a@example.test,+1-555-0101,85000,1 Main St,123456\nP-2,C-1,b@example.test,+1-555-0102,92000,2 Main St,234567\nP-3,C-2,c@example.test,+1-555-0103,78000,3 Main St,345678\n"
        analysis = analyze_csv(content)
        columns = {column["name"]: column for column in analysis["columns"]}
        self.assertEqual(columns["product_id"]["type"], "id")
        self.assertEqual(columns["product_id"]["key"], "PK")
        self.assertEqual(columns["customer_id"]["key"], "FK")
        self.assertTrue(all(columns[name]["pii"] for name in ("email", "phone", "salary", "home_address", "bank_account")))

    def test_prompt_targets_missing_phone_values(self):
        with patch.dict("core.os.environ", {"OPENROUTER_API_KEY":""}):
            proposal=analyze_prompt("Generate 1,000 e-commerce customers with 5% missing phone numbers")
        self.assertEqual(proposal["null_field"],"phone")
        cfg=Config.model_validate(proposal)
        tables,flags=generate_tabular(cfg)
        rows=next(iter(tables.values()))
        self.assertEqual(len(flags["missing"]),50)
        self.assertEqual(sum(row["phone"] is None for row in rows),50)
        self.assertFalse(any(row["email"] is None for row in rows))

    def test_invalid_openrouter_key_is_reported(self):
        failure=urllib.error.HTTPError("https://openrouter.ai/api/v1/chat/completions",401,"Unauthorized",{},None)
        with patch.dict("core.os.environ",{"OPENROUTER_API_KEY":"invalid-test-key"}), patch("core.urllib.request.urlopen",side_effect=failure):
            with self.assertRaisesRegex(ValueError,"rejected the API key"):
                analyze_prompt("Generate customer data")

    def test_seed_reproduces_identical_rows(self):
        cfg=Config(name="Customers",rows=1000,seed=42,columns=[Column(name="customer_id",type="id",key="PK"),Column(name="email",type="email",pii=True),Column(name="balance",type="decimal")])
        first,flags=generate_tabular(cfg)
        second,_=generate_tabular(cfg)
        self.assertEqual(first,second)
        self.assertEqual(len(flags["missing"]),30)
        self.assertEqual(len(flags["outlier"]),10)
        self.assertEqual(len(flags["duplicate"]),10)
        self.assertEqual(sum(any(value is None for value in row.values()) for row in next(iter(first.values()))),30)
        self.assertIsNone(validate(first,cfg,flags)["dimensions"]["Statistical similarity"])

    def test_relational_integrity_and_totals(self):
        cfg=Config(kind="relational",rows=250,seed=7)
        tables,flags=generate_relational(cfg)
        quality=validate(tables,cfg,flags)
        self.assertEqual(quality["dimensions"]["Referential integrity"],100)
        self.assertTrue(all(c["passed"] for c in quality["checks"]))
        document=make_document({"tables":tables,"config":cfg.model_dump()},"invoice")
        self.assertEqual(document["total"],sum(x["line_total"] for x in document["items"]))
        for fmt in ("json","csv","txt","pdf"):
            body,_,_=document_export_bytes(document,fmt)
            self.assertTrue(body)
        self.assertTrue(document_export_bytes(document,"pdf")[0].startswith(b"%PDF"))

    def test_banking_statement_reconciles(self):
        cfg=Config(kind="relational",domain="banking",rows=500,seed=23)
        tables,flags=generate_relational(cfg)
        report=validate(tables,cfg,flags)
        self.assertTrue(all(check["passed"] for check in report["checks"]))
        statement=make_document({"tables":tables,"config":cfg.model_dump()},"statement")
        self.assertEqual([t["date"] for t in statement["transactions"]],sorted(t["date"] for t in statement["transactions"]))
        balance=statement["opening_balance"]
        for transaction in statement["transactions"]:
            balance+=transaction["credit"]-transaction["debit"]
            self.assertEqual(balance,transaction["balance"])
        self.assertEqual(balance,statement["closing_balance"])

    def test_other_relational_domains_have_resolvable_keys(self):
        for domain in ("hospital","university","inventory","hr","hotel","library"):
            with self.subTest(domain=domain):
                cfg=Config(kind="relational",domain=domain,rows=50,seed=3)
                tables,flags=generate_relational(cfg)
                report=validate(tables,cfg,flags)
                self.assertEqual(report["dimensions"]["Referential integrity"],100)
                self.assertTrue(all(check["passed"] for check in report["checks"]))
                if domain=="library":
                    loan_dates={row["loan_id"]:row["loan_date"] for row in tables["loans"]}
                    self.assertTrue(all(row["return_date"]>=loan_dates[row["loan_id"]] for row in tables["returns"]))

    def test_sample_comparison_is_measured(self):
        sample=[{"value":str(v),"category":"A" if v<5 else "B"} for v in range(10)]
        cfg=Config(name="Sample",rows=200,seed=4,sample=sample,columns=[Column(name="value",type="decimal"),Column(name="category",type="category")])
        tables,flags=generate_tabular(cfg)
        report=validate(tables,cfg,flags)
        self.assertIsInstance(report["dimensions"]["Statistical similarity"],int)
        self.assertEqual(len(report["comparison"]),2)

    def test_version_history_preserves_seed(self):
        cfg=Config(name="Versioned",rows=20,seed=7)
        with tempfile.TemporaryDirectory() as temp, patch("core.DB_PATH",Path(temp)/"test.db"):
            init_db()
            first,flags=generate_tabular(cfg);quality=validate(first,cfg,flags)
            ident,_=save_dataset(cfg,first,quality,privacy_scan(cfg),flags)
            newer=cfg.model_copy(update={"seed":42})
            second,flags=generate_tabular(newer)
            version=save_version(ident,newer,second,validate(second,newer,flags),privacy_scan(newer),flags)
            self.assertEqual(version,2)
            self.assertEqual(get_dataset(ident,1)["tables"],first)
            self.assertEqual(get_dataset(ident,2)["tables"],second)
            self.assertEqual(get_dataset(ident,2)["flags"],flags)
            self.assertEqual([v["config"]["seed"] for v in list_versions(ident)],[42,7])

    def test_privacy_blocks_export(self):
        cfg=Config(columns=[Column(name="email",type="email",pii=True,privacy="none")])
        self.assertTrue(privacy_scan(cfg)["blocked"])
        with self.assertRaises(PermissionError):
            export_bytes({"name":"x","tables":{"x":[{"email":"a@example.test"}]},"privacy":privacy_scan(cfg)},"csv")

    def test_privacy_treatments_change_values_deterministically(self):
        cfg=Config(name="Private",rows=20,seed=9,columns=[Column(name="email",type="email",pii=True,privacy="mask"),Column(name="phone",type="phone",pii=True,privacy="hash"),Column(name="amount",type="decimal",pii=True,privacy="noise")])
        tables,flags=generate_tabular(cfg)
        same,_=generate_tabular(cfg)
        self.assertEqual(tables,same)
        row=next(iter(tables.values()))[0]
        self.assertIn("***",row["email"])
        self.assertEqual(len(row["phone"]),20)
        self.assertIsInstance(row["amount"],float)
        self.assertEqual(validate(tables,cfg,flags)["dimensions"]["Validity"],100)


if __name__=="__main__": unittest.main()
