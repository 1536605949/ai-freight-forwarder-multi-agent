from app.schemas import LeadCreate
from app.services.lead import score_lead
def test_lead_score(): assert score_lead(LeadCreate(company='A',email='a@b.com',trade_lane='CN-US',industry='furniture',country='US',source='referral',consent_status='opt_in'))>=80
