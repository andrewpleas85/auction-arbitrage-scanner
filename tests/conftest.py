import os

os.environ["SALES_TAX_PCT"] = "0"  # catalog tests assert exact dollar figures; the real default is applied on the page
os.environ["EBAY_FEE_PCT"] = "13.25"      # the old exact figures the catalog/page tests assert
os.environ["EBAY_ORDER_FEE"] = "0.30"
os.environ["RETURN_RESERVE_PCT"] = "0"
