REVIEW.md

I reviewed the starter `app/main.py` against the API contract and its
business rules.

  -----------------------------------------------------------------------------------------------------------------
                  Where (function / line)  What is wrong               How you'd notice  How I fixed it
                                                                      it (test, input,  
                                                                      or symptom)       
  -------------- ------------------------ --------------------------- ----------------- ---------------------------
  1              `compute_late_minutes`   It truncates elapsed        Test 09:40:00 and Apply the grace period to
                                          minutes before applying the 09:40:01 for a    elapsed seconds first, then
                                          10-minute grace period and  09:30 shift; test floor the remaining
                                          assumes the shift starts on overnight shifts. minutes; derive the correct
                                          the punch-in date.                            shift date.

  2              `compute_work_hours`     Python `round()` uses       Test a duration   Use `Decimal` with
                                          ties-to-even, not the       exactly halfway   `ROUND_HALF_UP`.
                                          required half-up rounding.  at two decimal    
                                                                      places.           

  3              `compute_overtime`       It counts overtime          Test punch-out 29 Apply the threshold and
                                          immediately after shift     and 30 minutes    calculate the correct
                                          end, without the 30-minute  after shift end;  shift-end instant.
                                          threshold, and mishandles   test an overnight 
                                          overnight shifts.           shift.            

  4              `EmployeeIn`             Dates and shift times are   Submit an invalid Add field validation and
                                          not validated.              date or `25:90`   return 422 for invalid
                                                                      as a shift time.  input.

  5              `create_employee`        The check-then-insert can   Send concurrent   Add a unique index, catch
                                          race, and `created_at` is a requests with the duplicate-key errors, and
                                          naive local datetime.       same `emp_code`;  store UTC timestamps.
                                                                      inspect the       
                                                                      stored timestamp. 

  6              `list_employees`         `skip = page * page_size`   Request page 1    Use
                                          skips the first page;       with size 20,     `(page - 1) * page_size`,
                                          `total` ignores the         then filter by    count the filtered query,
                                          department filter.          department and    and validate pagination.
                                          Pagination is unvalidated.  compare totals.   

  7              `punch_in`               The check-then-insert is    Send concurrent   Add a unique
                                          race-prone, missing         identical         `(emp_code, date)` index,
                                          employees are not handled   punches, use an   map duplicate-key errors to
                                          safely, and timestamps use  unknown employee, 409, return 404 for unknown
                                          local time.                 or test a fixed   employees, and normalize
                                                                      epoch timestamp.  timestamps.

  8              `PunchInIn` / `punch_in` `status` accepts arbitrary  Submit an invalid Validate allowed statuses
                                          strings; a falsey timestamp status or         and distinguish `None` from
                                          can be treated as omitted.  timestamp `0`.    supplied values; validate
                                                                                        epoch milliseconds.

  9              `list_attendance`        It loads all matching       Test a large      Sort, skip, and limit in
                                          records and sorts in Python collection,       MongoDB; count after
                                          before pagination, which    invalid dates,    filters and validate
                                          does not scale. Date and    and invalid page  inputs.
                                          pagination inputs are not   sizes.            
                                          validated.                                    

  10             Attendance response      The starter exposes MongoDB Inspect punch-in  Return only documented
                 serialization            `_id` as `id`, although the and               response fields and omit
                                          contract says internal IDs  attendance-list   internal IDs.
                                          must not appear in          responses.        
                                          responses.                                    

  11             Endpoint coverage        Punch-out, regularization,  Request each      Implement the routes
                                          monthly analytics,          documented route; according to
                                          department summary,         it returns 404.   `openapi.yaml`, including
                                          leaderboard, trend, and                       aggregation and explain
                                          explain endpoints are                         behavior.
                                          TODOs.                                        

  12             `health`                 It always returns `ok`      Stop MongoDB and  Check database connectivity
                                          without checking MongoDB,   call `/health`.   and report unavailability
                                          so it can report healthy                      when the ping fails.
                                          while the database is down.                   
  -----------------------------------------------------------------------------------------------------------------

    Looked at but not considered defects

-   `load_dotenv()` is reasonable for local development; environment
    variables can still configure deployment.
-   The default shift times are acceptable defaults, provided submitted
    values are validated.
-   HTTP 201 for successful employee creation and punch-in is
    appropriate. The defect was the lack of a race-safe guarantee, not
    the status code.
